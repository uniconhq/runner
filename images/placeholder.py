"""The entrypoint of an image whose program is not written yet. It names the
image and exits 0, so the image builds, runs and can be pulled by digest while
the real program is still to come.
"""

import sys


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "unicon-image"
    print(f"{name}: placeholder image, no program yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
