"""Hidden BAT bridge; project selection and service reuse belong to desktop.py."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'kicad_plugin'))
import desktop


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '--hidden':
        arguments.pop(0)
    if arguments and arguments[0] == '--desktop':
        arguments.pop(0)
    return desktop.main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
