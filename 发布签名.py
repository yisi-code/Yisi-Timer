"""yisi-定时工具 更新签名：生成密钥、签名发布包、核对签名。

用法（需要 cryptography，用打包用的那个 Python 环境跑即可）：
    python -I 发布签名.py keygen <密钥目录>
    python -I 发布签名.py sign <私钥 .pem> <安装包 exe> <版本，如 1.2.0>
    python -I 发布签名.py verify <公钥 .txt> <安装包 exe>

.sig 清单位于安装包旁边，名为 <安装包文件名>.sig，格式（UTF-8、LF）：
    yisi-timer update v1
    version: 1.2.0
    file: 安装 yisi-定时工具.exe
    size: 79752843
    sha256: <64 位十六进制>
    signature: <r 与 s 各 32 字节、大端拼接的十六进制>

签名覆盖 signature 之前的所有字节；程序里内置公钥（64 字节十六进制），
校验失败或大小/摘要不符就拒绝安装。
"""
import hashlib
import io
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HEADER = "yisi-timer update v1"
PRIVATE = "update-signing-private.pem"
PUBLIC = "update-signing-public.txt"


def public_hex(public_key):
    numbers = public_key.public_numbers()
    return numbers.x.to_bytes(32, "big").hex() + numbers.y.to_bytes(32, "big").hex()


def load_public(path):
    raw = bytes.fromhex(open(path, encoding="utf-8").read().strip())
    assert len(raw) == 64, "公钥应为 64 字节"
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + raw)


def keygen(directory):
    os.makedirs(directory, exist_ok=True)
    private_path = os.path.join(directory, PRIVATE)
    if os.path.exists(private_path):
        sys.exit(f"{private_path} 已存在，不覆盖")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    with open(private_path, "xb") as handle:
        handle.write(pem)
    with open(os.path.join(directory, PUBLIC), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(public_hex(key.public_key()) + "\n")
    print("私钥", private_path)
    print("公钥", public_hex(key.public_key()))


def manifest(archive, version):
    data = open(archive, "rb").read()
    return (
        f"{HEADER}\n"
        f"version: {version}\n"
        f"file: {os.path.basename(archive)}\n"
        f"size: {len(data)}\n"
        f"sha256: {hashlib.sha256(data).hexdigest()}\n"
    )


def sign(private_path, archive, version):
    key = serialization.load_pem_private_key(open(private_path, "rb").read(), password=None)
    body = manifest(archive, version)
    der = key.sign(body.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    text = body + f"signature: {r.to_bytes(32, 'big').hex()}{s.to_bytes(32, 'big').hex()}\n"
    with open(archive + ".sig", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    check(key.public_key(), archive)
    print(text, end="")


def check(public_key, archive):
    text = open(archive + ".sig", encoding="utf-8").read()
    body, _, signature_line = text.rpartition("signature: ")
    raw = bytes.fromhex(signature_line.strip())
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public_key.verify(signature, body.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    fields = dict(line.split(": ", 1) for line in body.splitlines()[1:])
    data = open(archive, "rb").read()
    assert fields["file"] == os.path.basename(archive), "文件名不符"
    assert int(fields["size"]) == len(data), "大小不符"
    assert fields["sha256"] == hashlib.sha256(data).hexdigest(), "SHA256 不符"
    print("签名核对通过")


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "keygen":
        keygen(sys.argv[2])
    elif command == "sign":
        sign(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "verify":
        check(load_public(sys.argv[2]), sys.argv[3])
    else:
        sys.exit(__doc__)
