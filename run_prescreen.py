import sys
from momni.prescreen.joint import main

if __name__ == "__main__":
    if len(sys.argv) == 1 or any(a in ("--help", "-h") for a in sys.argv[1:]):
        main(["--help"])
    elif not any(a in ("--input", "--bundle") or a.startswith(("--input=", "--bundle=")) for a in sys.argv[1:]):
        sys.exit("Provide --input candidates.jsonl (or an explicit --bundle). See README.md")
    else:
        main()
