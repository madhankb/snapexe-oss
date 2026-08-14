import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import delete_all


def test_parse_delete_accepts_secret_id():
    args = delete_all.parse_arguments(["--tag", "prod", "--secret-id", "snapexe/opensearch"])
    assert args.secret_id == "snapexe/opensearch"
