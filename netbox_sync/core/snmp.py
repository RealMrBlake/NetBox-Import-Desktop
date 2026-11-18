"""Small SNMP client (v2c and v3 USM) for reading devices – no external SNMP library.

Supports GET and GETBULK walks. SNMPv3 handles engine discovery, time
synchronisation, authentication (MD5, SHA-1, SHA-224/256/384/512) and privacy
(DES, AES-128 and AES-192/256 with Blumenthal key extension) using the
``cryptography`` package that paramiko already brings along.
"""

import hashlib
import hmac
import itertools
import random
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:  # cryptography >= 43 moved TripleDES (used for single DES with K1=K2=K3)
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # pragma: no cover - older cryptography
    TripleDES = algorithms.TripleDES

PORT = 161

# ASN.1 / SNMP tags
INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
IP_ADDRESS, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW = 0x80, 0x81, 0x82
GET, GET_NEXT, RESPONSE, GET_BULK, REPORT = 0xA0, 0xA1, 0xA2, 0xA5, 0xA8

AUTH_PROTOCOLS = {  # name -> (hash, length of the truncated HMAC)
    "MD5": (hashlib.md5, 12), "SHA": (hashlib.sha1, 12), "SHA-224": (hashlib.sha224, 16),
    "SHA-256": (hashlib.sha256, 24), "SHA-384": (hashlib.sha384, 32), "SHA-512": (hashlib.sha512, 48),
}
PRIV_PROTOCOLS = {"DES": 8, "AES": 16, "AES-192": 24, "AES-256": 32}  # name -> key length

REPORT_TEXT = {
    "1.3.6.1.6.3.15.1.1.1.0": "Sicherheitsstufe wird nicht unterstützt",
    "1.3.6.1.6.3.15.1.1.2.0": "nicht im Zeitfenster",
    "1.3.6.1.6.3.15.1.1.3.0": "unbekannter Benutzer",
    "1.3.6.1.6.3.15.1.1.4.0": "unbekannte Engine-ID",
    "1.3.6.1.6.3.15.1.1.5.0": "falsches Authentifizierungs-Passwort oder -Protokoll",
    "1.3.6.1.6.3.15.1.1.6.0": "Entschlüsselung fehlgeschlagen (Privacy-Passwort oder -Protokoll falsch)",
}


class SnmpError(Exception):
    """Base error."""


class SnmpTimeout(SnmpError):
    """No answer – host down, SNMP off or (for v2c) wrong community."""


class SnmpAuthError(SnmpError):
    """The agent answered but rejected the credentials (SNMPv3 report)."""


# ----------------------------------------------------------------------
# BER encoding
# ----------------------------------------------------------------------

def _length(size):
    if size < 0x80:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag, content):
    return bytes([tag]) + _length(len(content)) + content


def enc_int(value, tag=INTEGER):
    size = max(1, (value.bit_length() + 8) // 8)
    return tlv(tag, value.to_bytes(size, "big", signed=True))


def enc_str(value):
    return tlv(OCTET_STRING, value if isinstance(value, bytes) else str(value).encode())


def enc_oid(oid):
    parts = [int(part) for part in str(oid).strip(".").split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(chunk))
    return tlv(OID, bytes(body))


def seq(*items):
    return tlv(SEQUENCE, b"".join(items))


def decode(data, pos=0):
    """``(tag, content, next position)`` of the TLV at ``pos``."""
    if pos + 2 > len(data):
        raise SnmpError("Antwort unvollständig")
    tag, size = data[pos], data[pos + 1]
    pos += 2
    if size & 0x80:
        count = size & 0x7F
        size = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + size > len(data):
        raise SnmpError("Antwort unvollständig")
    return tag, data[pos:pos + size], pos + size


def children(content):
    items, pos = [], 0
    while pos < len(content):
        tag, value, pos = decode(content, pos)
        items.append((tag, value))
    return items


def dec_oid(content):
    first = content[0]
    parts = [first // 40 if first < 80 else 2, first % 40 if first < 80 else first - 80]
    value = 0
    for byte in content[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return ".".join(map(str, parts))


def dec_value(tag, content):
    if tag == INTEGER:
        return int.from_bytes(content, "big", signed=True)
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(content, "big")
    if tag == OID:
        return dec_oid(content)
    if tag == IP_ADDRESS:
        return ".".join(str(byte) for byte in content)
    if tag in (OCTET_STRING, OPAQUE):
        return bytes(content)
    return None  # NULL, noSuchObject, noSuchInstance, endOfMibView


def oid_key(oid):
    return tuple(int(part) for part in oid.strip(".").split("."))


def in_subtree(oid, root):
    return oid_key(oid)[:len(oid_key(root))] == oid_key(root)


def text(value):
    """OCTET STRING as readable text (trailing NULs and control characters removed)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "cp1252"):
            try:
                value = value.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return "".join(char for char in str(value) if char.isprintable()).strip()


# ----------------------------------------------------------------------
# PDUs
# ----------------------------------------------------------------------

def _pdu(kind, request_id, oids, non_repeaters=0, max_repetitions=0):
    varbinds = seq(*(seq(enc_oid(oid), tlv(NULL, b"")) for oid in oids))
    return tlv(kind, enc_int(request_id) + enc_int(non_repeaters) + enc_int(max_repetitions) + varbinds)


def _parse_pdu(tag, content):
    request_id, status, index, varbinds = children(content)
    bindings = []
    for _tag, binding in children(varbinds[1]):
        (_oid_tag, oid_raw), (value_tag, value_raw) = children(binding)
        bindings.append((dec_oid(oid_raw), value_tag, dec_value(value_tag, value_raw)))
    return {"type": tag, "request_id": dec_value(INTEGER, request_id[1]), "error_status": dec_value(INTEGER, status[1]),
            "error_index": dec_value(INTEGER, index[1]), "varbinds": bindings}


# ----------------------------------------------------------------------
# SNMPv3 keys and crypto
# ----------------------------------------------------------------------

_KEY_CACHE = {}


def aes_cfb(key, iv, data, decrypt=False):
    """AES-CFB128 (RFC 3826) built on ECB – the CFB mode of ``cryptography`` is being deprecated."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out, register = bytearray(), iv
    for start in range(0, len(data), 16):
        block = data[start:start + 16]
        stream = encryptor.update(register)
        chunk = bytes(a ^ b for a, b in zip(block, stream))
        out += chunk
        register = (block if decrypt else chunk).ljust(16, b"\0")
    return bytes(out)


def password_to_key(password, hash_func):
    """RFC 3414 A.2: hash of the password repeated to 1 MB."""
    key = (password, hash_func().name)
    if key not in _KEY_CACHE:
        data = password.encode()
        if not data:
            raise SnmpError("leeres SNMPv3-Passwort")
        digest = hash_func()
        repeated = (data * (64 // len(data) + 2))
        chunk = (repeated * (1048576 // len(repeated) + 1))[:1048576]
        digest.update(chunk)
        _KEY_CACHE[key] = digest.digest()
    return _KEY_CACHE[key]


def localize(key, engine_id, hash_func):
    return hash_func(key + engine_id + key).digest()


def priv_key(password, engine_id, hash_func, size):
    key = localize(password_to_key(password, hash_func), engine_id, hash_func)
    while len(key) < size:  # Blumenthal key extension for AES-192/256
        key += hash_func(key).digest()
    return key[:size] if size > 8 else key[:16]  # DES needs key + pre-IV


class Credential:
    """One set of credentials: ``{"version": "2c", "community": …}`` or a v3 user."""

    def __init__(self, record):
        self.record = record
        self.version = str(record.get("version") or "2c")
        self.community = record.get("community") or "public"
        self.username = record.get("username") or ""
        self.auth_protocol = record.get("auth_protocol") or ""
        self.auth_password = record.get("auth_password") or ""
        self.priv_protocol = record.get("priv_protocol") or ""
        self.priv_password = record.get("priv_password") or ""
        self.context = record.get("context") or ""
        if self.version == "3":
            if self.auth_protocol and self.auth_protocol not in AUTH_PROTOCOLS:
                raise SnmpError(f"unbekanntes Authentifizierungs-Protokoll {self.auth_protocol}")
            if self.priv_protocol and self.priv_protocol not in PRIV_PROTOCOLS:
                raise SnmpError(f"unbekanntes Privacy-Protokoll {self.priv_protocol}")
            if self.priv_protocol and not self.auth_protocol:
                raise SnmpError("Privacy ohne Authentifizierung ist bei SNMPv3 nicht möglich")

    @property
    def label(self):
        if self.version != "3":
            return "SNMPv2c"
        level = ("authPriv" if self.priv_protocol else "authNoPriv") if self.auth_protocol else "noAuthNoPriv"
        return f"SNMPv3 {self.username} ({level})"


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------

_ids = itertools.count(random.randint(1, 1 << 30))


class SnmpClient:
    def __init__(self, host, credential, timeout=1.0, retries=1, port=PORT):
        self.host, self.port = host, port
        self.credential = credential if isinstance(credential, Credential) else Credential(credential)
        self.timeout, self.retries = timeout, retries
        self.engine_id = b""
        self.boots = self.engine_time = 0
        self._time_base = 0.0
        self._salt = random.getrandbits(64)
        self.bulk = True

    # transport ----------------------------------------------------------

    def _exchange(self, packet, accept):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            for _attempt in range(self.retries + 1):
                try:
                    sock.sendto(packet, (self.host, self.port))
                except OSError as error:
                    raise SnmpTimeout(str(error)) from error
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        data, _addr = sock.recvfrom(65535)
                    except (socket.timeout, ConnectionResetError):
                        break
                    except OSError:
                        break
                    try:
                        result = accept(data)
                    except SnmpAuthError:
                        raise
                    except (SnmpError, ValueError, IndexError):
                        continue
                    if result is not None:
                        return result
        raise SnmpTimeout(f"{self.host}: keine SNMP-Antwort")

    # v2c ----------------------------------------------------------------

    def _request_v2(self, pdu_type, oids, non_repeaters=0, max_repetitions=0):
        request_id = next(_ids) & 0x7FFFFFFF
        community = self.credential.community.encode()
        packet = seq(enc_int(1), enc_str(community), _pdu(pdu_type, request_id, oids, non_repeaters, max_repetitions))

        def accept(data):
            _tag, content, _pos = decode(data)
            version, community_raw, (pdu_tag, pdu_content) = children(content)
            if community_raw[1] != community:
                return None
            pdu = _parse_pdu(pdu_tag, pdu_content)
            return pdu if pdu["request_id"] == request_id else None
        return self._exchange(packet, accept)

    # v3 -----------------------------------------------------------------

    def _hash(self):
        return AUTH_PROTOCOLS[self.credential.auth_protocol][0] if self.credential.auth_protocol else None

    def _discover(self):
        msg_id = next(_ids) & 0x7FFFFFFF
        usm = seq(enc_str(b""), enc_int(0), enc_int(0), enc_str(b""), enc_str(b""), enc_str(b""))
        scoped = seq(enc_str(b""), enc_str(b""), _pdu(GET, msg_id, []))
        packet = seq(enc_int(3), seq(enc_int(msg_id), enc_int(65507), enc_str(b"\x04"), enc_int(3)),
                     enc_str(usm), scoped)

        message = self._exchange(packet, lambda data: self._parse_v3(data, msg_id, verify=False))
        if not message["engine_id"]:
            raise SnmpError(f"{self.host}: keine SNMPv3-Engine-ID erhalten")
        self._set_engine(message)

    def _set_engine(self, message):
        self.engine_id = message["engine_id"]
        self.boots, self.engine_time = message["boots"], message["time"]
        self._time_base = time.monotonic()
        hash_func = self._hash()
        cred = self.credential
        self._auth_key = localize(password_to_key(cred.auth_password, hash_func), self.engine_id, hash_func) \
            if hash_func else None
        self._priv_key = priv_key(cred.priv_password, self.engine_id, hash_func, PRIV_PROTOCOLS[cred.priv_protocol]) \
            if cred.priv_protocol else None

    def _now(self):
        return self.engine_time + int(time.monotonic() - self._time_base)

    def _encrypt(self, plain, now):
        cred = self.credential
        self._salt = (self._salt + 1) & 0xFFFFFFFFFFFFFFFF
        if cred.priv_protocol == "DES":
            salt = struct.pack(">II", self.boots & 0xFFFFFFFF, self._salt & 0xFFFFFFFF)
            iv = bytes(a ^ b for a, b in zip(self._priv_key[8:16], salt))
            plain += b"\x00" * (-len(plain) % 8)
            encryptor = Cipher(TripleDES(self._priv_key[:8] * 3), modes.CBC(iv)).encryptor()
        else:
            salt = struct.pack(">Q", self._salt)
            iv = struct.pack(">II", self.boots, now) + salt
            return aes_cfb(self._priv_key, iv, plain), salt
        return encryptor.update(plain) + encryptor.finalize(), salt

    def _decrypt(self, data, salt, boots, engine_time):
        if self.credential.priv_protocol == "DES":
            iv = bytes(a ^ b for a, b in zip(self._priv_key[8:16], salt))
            decryptor = Cipher(TripleDES(self._priv_key[:8] * 3), modes.CBC(iv)).decryptor()
        else:
            iv = struct.pack(">II", boots, engine_time) + salt
            return aes_cfb(self._priv_key, iv, data, decrypt=True)
        return decryptor.update(data) + decryptor.finalize()

    def _parse_v3(self, data, msg_id, verify=True):
        _tag, content, _pos = decode(data)
        version, header, (_usm_tag, usm_raw), (data_tag, data_raw) = children(content)
        if dec_value(INTEGER, version[1]) != 3:
            raise SnmpError("keine SNMPv3-Antwort")
        header_items = children(header[1])
        if dec_value(INTEGER, header_items[0][1]) != msg_id:
            return None
        flags = header_items[2][1][0] if header_items[2][1] else 0
        _t, usm_content, _p = decode(usm_raw)
        engine_id, boots, engine_time, user, auth_params, priv_params = (item[1] for item in children(usm_content))
        message = {"engine_id": bytes(engine_id), "boots": dec_value(INTEGER, boots),
                   "time": dec_value(INTEGER, engine_time)}
        if verify and flags & 0x01 and self._auth_key:
            hash_func, size = AUTH_PROTOCOLS[self.credential.auth_protocol]
            position = data.find(auth_params)
            zeroed = data[:position] + b"\x00" * len(auth_params) + data[position + len(auth_params):]
            if hmac.new(self._auth_key, zeroed, hash_func).digest()[:size] != auth_params:
                raise SnmpError("Antwort mit ungültiger Signatur")
        if data_tag == OCTET_STRING:
            if not self._priv_key:
                raise SnmpError("verschlüsselte Antwort ohne Privacy-Schlüssel")
            plain = self._decrypt(bytes(data_raw), bytes(priv_params), message["boots"], message["time"])
            _t, scoped, _p = decode(plain)
        else:
            scoped = data_raw
        _context_engine, _context_name, (pdu_tag, pdu_content) = children(scoped)
        message["pdu"] = _parse_pdu(pdu_tag, pdu_content)
        return message

    def _request_v3(self, pdu_type, oids, non_repeaters=0, max_repetitions=0, resync=True):
        if not self.engine_id:
            self._discover()
        cred = self.credential
        msg_id = next(_ids) & 0x7FFFFFFF
        pdu = _pdu(pdu_type, msg_id, oids, non_repeaters, max_repetitions)
        scoped = seq(enc_str(self.engine_id), enc_str(cred.context), pdu)
        flags = 0x04 | (0x01 if cred.auth_protocol else 0) | (0x02 if cred.priv_protocol else 0)
        salt, now = b"", self._now()
        if cred.priv_protocol:
            encrypted, salt = self._encrypt(scoped, now)
            body = enc_str(encrypted)
        else:
            body = scoped
        auth_size = AUTH_PROTOCOLS[cred.auth_protocol][1] if cred.auth_protocol else 0

        def build(auth_params):
            usm = seq(enc_str(self.engine_id), enc_int(self.boots), enc_int(now), enc_str(cred.username),
                      enc_str(auth_params), enc_str(salt))
            return seq(enc_int(3), seq(enc_int(msg_id), enc_int(65507), enc_str(bytes([flags])), enc_int(3)),
                       enc_str(usm), body)
        packet = build(b"\x00" * auth_size)
        if cred.auth_protocol:
            hash_func, size = AUTH_PROTOCOLS[cred.auth_protocol]
            packet = build(hmac.new(self._auth_key, packet, hash_func).digest()[:size])

        def accept(data):
            return self._parse_v3(data, msg_id)
        message = self._exchange(packet, accept)
        pdu = message["pdu"]
        if pdu["type"] == REPORT:
            oid = pdu["varbinds"][0][0] if pdu["varbinds"] else ""
            if oid == "1.3.6.1.6.3.15.1.1.2.0" and resync:  # not in time window: resynchronise once
                self.boots, self.engine_time = message["boots"], message["time"]
                self._time_base = time.monotonic()
                return self._request_v3(pdu_type, oids, non_repeaters, max_repetitions, resync=False)
            raise SnmpAuthError(f"SNMPv3 {cred.username}: {REPORT_TEXT.get(oid, 'abgelehnt (' + oid + ')')}")
        return pdu

    # public API ---------------------------------------------------------

    def request(self, pdu_type, oids, non_repeaters=0, max_repetitions=0):
        if self.credential.version == "3":
            return self._request_v3(pdu_type, oids, non_repeaters, max_repetitions)
        return self._request_v2(pdu_type, oids, non_repeaters, max_repetitions)

    def get(self, oids):
        """``{oid: value}``; missing objects are left out."""
        result = {}
        oids = list(oids)
        for start in range(0, len(oids), 20):
            pdu = self.request(GET, oids[start:start + 20])
            if pdu["error_status"]:
                # v1-style error for one bad OID: fall back to single requests
                for oid in oids[start:start + 20]:
                    single = self.request(GET, [oid])
                    if not single["error_status"]:
                        result.update({o: v for o, tag, v in single["varbinds"] if tag < NO_SUCH_OBJECT})
                continue
            result.update({oid: value for oid, tag, value in pdu["varbinds"] if tag < NO_SUCH_OBJECT})
        return result

    def walk(self, root, limit=2000):
        """``{oid: value}`` of the subtree below ``root``."""
        result, current = {}, root
        while len(result) < limit:
            if self.bulk:
                pdu = self.request(GET_BULK, [current], 0, 25)
                if pdu["error_status"]:
                    self.bulk = False
                    continue
            else:
                pdu = self.request(GET_NEXT, [current])
                if pdu["error_status"]:
                    break
            if not pdu["varbinds"]:
                break
            done = False
            for oid, tag, value in pdu["varbinds"]:
                if tag == END_OF_MIB_VIEW or not in_subtree(oid, root) or oid_key(oid) <= oid_key(current):
                    done = True
                    break
                result[oid] = value
                current = oid
            if done:
                break
        return result


def connect(host, credentials, timeout=1.0, retries=1, check_oid="1.3.6.1.2.1.1.2.0", port=PORT):
    """First credential that works for ``host``: ``(client, notes)``.

    ``notes`` collects why other credentials failed (e.g. a wrong v3 password),
    which helps to spot misconfigured devices. Raises ``SnmpTimeout`` when no
    credential got an answer.
    """
    notes = []
    for record in credentials:
        try:
            client = SnmpClient(host, record, timeout, retries, port)
            if client.get([check_oid]):
                return client, notes
            notes.append(f"{client.credential.label}: keine Daten")
        except SnmpAuthError as error:
            notes.append(str(error))
        except SnmpTimeout:
            continue
        except SnmpError as error:
            notes.append(str(error))
    if notes:
        raise SnmpAuthError("; ".join(notes))
    raise SnmpTimeout(f"{host}: keine SNMP-Antwort")


def table(walked, root):
    """Group a walked table by row index: ``{index tuple: {column: value}}``."""
    rows = {}
    base = len(oid_key(root))
    for oid, value in walked.items():
        key = oid_key(oid)
        if len(key) <= base + 1:
            continue
        rows.setdefault(key[base + 1:], {})[key[base]] = value
    return rows

