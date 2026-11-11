"""Create only disposable test configuration; never read production credentials."""

import json
import os
import secrets
from pathlib import Path


ROOT = Path("/opt/sub2api/work/samlab-admission-20261111T071145Z")


def main():
    if ROOT.resolve() != ROOT or not (ROOT / "isolated.compose.yml").is_file():
        raise RuntimeError("unexpected test workspace")
    os.umask(0o077)
    path = ROOT / "runtime.env"
    if path.exists():
        raise RuntimeError("test configuration already exists; will not overwrite it")
    password = secrets.token_hex(24)
    values = {
        "DATABASE_PASSWORD": password,
        "REDIS_PASSWORD": secrets.token_hex(24),
        "ADMIN_EMAIL": "isolated-admin@example.invalid",
        "ADMIN_PASSWORD": secrets.token_hex(24),
        "JWT_SECRET": secrets.token_hex(32),
        "LAB_ADMISSION_ENABLED": "1",
        "LAB_ADMISSION_URL": "http://127.0.0.1:18011",
        "LAB_ADMISSION_ALLOW_HTTP": "1",
        "SAMLAB_INTEGRATION_SECRET": secrets.token_hex(32),
        "LAB_ADMISSION_DATABASE_URL":
            f"postgres://labtest:{password}@127.0.0.1:55433/labtest?sslmode=disable",
    }
    with path.open("x", encoding="utf-8") as output:
        output.write("".join(f"{key}={value}\n" for key, value in values.items()))
    os.chmod(path, 0o600)
    print(json.dumps({"configuration_created": True, "production_changed": False}))


if __name__ == "__main__":
    main()
