"""Minimal SNMP agent on localhost for tests (v2c and v3 USM)."""

import hmac
import socket
import struct
import threading

from netbox_sync.core import snmp
from netbox_sync.core.snmp import (
    AUTH_PROTOCOLS,
    GET,
    GET_BULK,
    GET_NEXT,
    INTEGER,
    OCTET_STRING,
    PRIV_PROTOCOLS,
    REPORT,
    RESPONSE,
    TripleDES,
    children,
    dec_oid,
    dec_value,
    decode,
    enc_int,
    enc_oid,
    enc_str,
    localize,
    oid_key,
    password_to_key,
    priv_key,
    seq,
    tlv,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ENGINE_ID = bytes.fromhex("80001f8880c71100000000000000000001")


def encode_value(value):
    if isinstance(value, bool):
        return enc_int(int(value))
    if isinstance(value, int):
        return enc_int(value)
    if isinstance(value, tuple) and value[0] == "oid":
        return enc_oid(value[1])
    if isinstance(value, tuple) and value[0] == "ip":
        return tlv(snmp.IP_ADDRESS, bytes(int(part) for part in value[1].split(".")))
    if isinstance(value, tuple) and value[0] == "counter":
        return enc_int(value[1], snmp.COUNTER32)
    return enc_str(value)


class Agent:
    def __init__(self, mib, community="public", users=None):
        self.mib = {oid.strip("."): value for oid, value in mib.items()}
        self.order = sorted(self.mib, key=oid_key)
        self.community = community.encode()
        self.users = users or {}  # name -> credential record
        self.boots, self.time = 3, 1000
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self.sock.close()

    # MIB --------------------------------------------------------------

    def _next(self, oid):
        key = oid_key(oid)
        return next((item for item in self.order if oid_key(item) > key), None)

    def _bindings(self, pdu_type, oids, non_repeaters, repetitions):
        result = []
        if pdu_type == GET:
            for oid in oids:
                value = self.mib.get(oid)
                result.append(seq(enc_oid(oid), encode_value(value) if value is not None
                                  else tlv(snmp.NO_SUCH_OBJECT, b"")))
        else:
            count = repetitions if pdu_type == GET_BULK else 1
            for oid in oids:
                current = oid
                for _ in range(max(1, count)):
                    nxt = self._next(current)
                    if nxt is None:
                        result.append(seq(enc_oid(current), tlv(snmp.END_OF_MIB_VIEW, b"")))
                        break
                    result.append(seq(enc_oid(nxt), encode_value(self.mib[nxt])))
                    current = nxt
        return seq(*result)

    def _response(self, request_id, bindings, kind=RESPONSE):
        return tlv(kind, enc_int(request_id) + enc_int(0) + enc_int(0) + bindings)

    @staticmethod
    def _read_pdu(tag, content):
        request_id, a, b, varbinds = children(content)
        oids = [dec_oid(children(binding)[0][1]) for _t, binding in children(varbinds[1])]
        return tag, dec_value(INTEGER, request_id[1]), dec_value(INTEGER, a[1]), dec_value(INTEGER, b[1]), oids

    # serving ----------------------------------------------------------

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            self.requests += 1
            try:
                answer = self._handle(data)
            except Exception:  # noqa: BLE001 - ignore garbage like a real agent
                answer = None
            if answer:
                self.sock.sendto(answer, addr)

    def _handle(self, data):
        _tag, content, _pos = decode(data)
        items = children(content)
        version = dec_value(INTEGER, items[0][1])
        if version == 1:
            if items[1][1] != self.community:
                return None
            tag, request_id, a, b, oids = self._read_pdu(*items[2])
            bindings = self._bindings(tag, oids, a, b)
            return seq(enc_int(1), enc_str(self.community), self._response(request_id, bindings))
        return self._handle_v3(data, items)

    def _handle_v3(self, data, items):
        _v, header, (_t, usm_raw), (data_tag, data_raw) = items
        header_items = children(header[1])
        msg_id = dec_value(INTEGER, header_items[0][1])
        flags = header_items[2][1][0]
        _t, usm_content, _p = decode(usm_raw)
        engine_id, _boots, _time, user, auth_params, priv_params = (item[1] for item in children(usm_content))
        user = bytes(user).decode()

        def report(oid):
            body = seq(enc_str(ENGINE_ID), enc_str(b""), self._response(
                msg_id, seq(seq(enc_oid(oid), enc_int(1, snmp.COUNTER32))), REPORT))
            usm = seq(enc_str(ENGINE_ID), enc_int(self.boots), enc_int(self.time), enc_str(user), enc_str(b""),
                      enc_str(b""))
            return seq(enc_int(3), seq(enc_int(msg_id), enc_int(65507), enc_str(b"\x00"), enc_int(3)),
                       enc_str(usm), body)

        if not engine_id:
            return report("1.3.6.1.6.3.15.1.1.4.0")
        record = self.users.get(user)
        if record is None:
            return report("1.3.6.1.6.3.15.1.1.3.0")
        hash_func, size = AUTH_PROTOCOLS[record["auth_protocol"]]
        auth_key = localize(password_to_key(record["auth_password"], hash_func), ENGINE_ID, hash_func)
        position = data.find(auth_params)
        zeroed = data[:position] + b"\x00" * len(auth_params) + data[position + len(auth_params):]
        if not flags & 1 or hmac.new(auth_key, zeroed, hash_func).digest()[:size] != auth_params:
            return report("1.3.6.1.6.3.15.1.1.5.0")
        protocol = record.get("priv_protocol")
        key = priv_key(record["priv_password"], ENGINE_ID, hash_func, PRIV_PROTOCOLS[protocol]) if protocol else None
        if data_tag == OCTET_STRING:
            plain = self._crypt(protocol, key, bytes(data_raw), bytes(priv_params), _boots, _time, decrypt=True)
            _t, scoped, _p = decode(plain)
        else:
            scoped = data_raw
        _ce, _cn, pdu = children(scoped)
        tag, request_id, a, b, oids = self._read_pdu(*pdu)
        scoped_out = seq(enc_str(ENGINE_ID), enc_str(b""), self._response(request_id, self._bindings(tag, oids, a, b)))
        salt = b""
        if protocol:
            salt = struct.pack(">Q", 99) if protocol != "DES" else struct.pack(">II", self.boots, 77)
            encrypted = self._crypt(protocol, key, scoped_out, salt, dec_value(INTEGER, enc_int(self.boots)[2:]),
                                    self.time, decrypt=False)
            body = enc_str(encrypted)
        else:
            body = scoped_out
        out_flags = flags & 0x03

        def build(params):
            usm = seq(enc_str(ENGINE_ID), enc_int(self.boots), enc_int(self.time), enc_str(user), enc_str(params),
                      enc_str(salt))
            return seq(enc_int(3), seq(enc_int(msg_id), enc_int(65507), enc_str(bytes([out_flags])), enc_int(3)),
                       enc_str(usm), body)
        packet = build(b"\x00" * size)
        return build(hmac.new(auth_key, packet, hash_func).digest()[:size])

    @staticmethod
    def _crypt(protocol, key, data, salt, boots, engine_time, decrypt):
        if protocol == "DES":
            iv = bytes(a ^ b for a, b in zip(key[8:16], salt))
            cipher = Cipher(TripleDES(key[:8] * 3), modes.CBC(iv))
            if not decrypt:
                data += b"\x00" * (-len(data) % 8)
        else:
            boots = boots if isinstance(boots, int) else dec_value(INTEGER, boots)
            engine_time = engine_time if isinstance(engine_time, int) else dec_value(INTEGER, engine_time)
            return snmp.aes_cfb(key, struct.pack(">II", boots, engine_time) + salt, data, decrypt)
        worker = cipher.decryptor() if decrypt else cipher.encryptor()
        return worker.update(data) + worker.finalize()
