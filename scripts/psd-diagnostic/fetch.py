"""Fork-only diagnostic: fetch the unchanged PhotoCraft manifest sets without compiling xtask.
Source: photocraft@74330157cb601573ae4523a95bb9132822fc3918, corpus_pins.rs.
No fixture code is executed. Every fixture must match its committed SHA256.
"""
import concurrent.futures
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
import urllib.parse
import urllib.request

PSD = ("psd-tools/psd-tools", "96eb134c17b2c65edf4c4151c0f00b802ada86c2", "tests/psd_files")
AG = ("Agamnentzar/ag-psd", "387049670cb89b88fb8fe1b7c01aeacf98dd2e3b", "test")
ATN = {
    "sbaril": ("sbaril/Photoshop-Scripts", "622493c9c617c9d2ff38500e8e512c0ad421eed9", "Actions"),
    "bjango": ("bjango/Bjango-Actions", "0fdc790b4931fd36816e6056dd84c2aa0d3ff054", ""),
    "bkwld": ("BKWLD/photoshop-actions", "d1a5d24e02bf23f78659106473ef7d8c747bea00", ""),
    "atntools": ("mrijk/atntools", "529db30b631f32bc7970121af0393853c2154fa0", ""),
    "trimmer": ("damsmcfly/photoshop-cleaner-trimmer", "fd3814df2a625ed7a015633eb91d8fe2f5bbe9a0", ""),
    "alpha-utility": ("uclagamelab/AlphaUtilityAdditions", "7bdae950a7db4c6c2224bcaf08f4c17ec18958cb", ""),
    "gif-creator": ("overbyte/photoshop-gif-creator-script", "9fb78e8c09a21678afc84ccb31a435bd242894df", ""),
}
MANIFESTS = {
    "psd": (170, "372f7475e93da20365a7a4d87bd4571c9ae39a8528c2bee18c016d5e10019944"),
    "psd-tools": (309, "a0ada205456706fafc920910ae370b81415d002b9ed7d4d7e4fb9a88c31a00c7"),
    "atn": (16, "76e5b503d91a5894b8d28b06c141d478c5620c1b25754994fe056fc52b65f483"),
}
root = Path(tempfile.mkdtemp(prefix="psd-corpus-", dir=os.environ["RUNNER_TEMP"]))
tasks = []
for group, (count, digest) in MANIFESTS.items():
    raw = Path(__file__).with_name(group + ".sha256").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest, "manifest changed"
    seen = set()
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        sha, name = line.split("  ", 1)
        path = PurePosixPath(name)
        assert not path.is_absolute() and ".." not in path.parts and "\\" not in name
        assert name not in seen and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
        seen.add(name)
        if group == "psd-tools":
            source, relative = PSD, name
        else:
            prefix, relative = name.split("/", 1)
            source = ({"psd-tools": PSD, "ag-psd": AG} if group == "psd" else ATN)[prefix]
        repo, commit, subdir = source
        remote = "/".join(part for part in (subdir, relative) if part)
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/" + urllib.parse.quote(remote, safe="/")
        tasks.append((url, root / group / name, sha))
    assert len(seen) == count, (group, len(seen), count)

def fetch(task):
    url, dest, expected = task
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            digest = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=30) as response, dest.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"checksum mismatch: {dest}")
            return dest.stat().st_size
        except Exception:
            dest.unlink(missing_ok=True)
            if attempt:
                raise

with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    sizes = list(pool.map(fetch, tasks))
assert len(sizes) == 495
with open(os.environ["GITHUB_ENV"], "a") as env:
    env.write(f"CRAFT_PSD_CORPUS={root}\n")
print(f"Verified all {len(sizes)} corpus files ({sum(sizes)} bytes) at {root}", flush=True)
