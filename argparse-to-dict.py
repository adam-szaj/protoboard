import argparse
import json
import re
from typing import Any


def _strip_ansi(text: str) -> str:
    """Usuwa sekwencje sterujące ANSI z tekstu."""
    if not text:
        return text
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def argparse_to_dict(
    parser: argparse.ArgumentParser, prog_name: str | None = None
) -> dict[str, Any]:
    """Rekurencyjnie konwertuje ArgumentParser do słownika."""
    # Wyłącz formatowanie w parserie (usuwa sekwencje ANSI)
    original_formatter = parser.formatter_class
    parser.formatter_class = argparse.RawDescriptionHelpFormatter

    usage = _strip_ansi(parser.format_usage().strip())

    # Przywróć oryginalny formatter
    parser.formatter_class = original_formatter

    result = {
        "prog": prog_name or parser.prog,
        "description": _strip_ansi(parser.description or ""),
        "usage": usage,
        "arguments": [],
        "subcommands": {},
    }

    # Akcje (opcje i pozycyjne)
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            # Subkomendy
            for cmd_name, subparser in action.choices.items():
                result["subcommands"][cmd_name] = argparse_to_dict(subparser, cmd_name)
        else:
            type_name = "str"
            if action.type:
                type_name = getattr(action.type, "__name__", str(action.type))

            arg_info = {
                "dest": action.dest,
                "option_strings": action.option_strings or [],
                "type": type_name,
                "help": action.help,
                "default": action.default,
                "required": action.required,
                "nargs": action.nargs,
                "choices": action.choices,
            }
            result["arguments"].append(arg_info)

    return result


def main():
    # Przykład: parser z subkomendami
    parser = argparse.ArgumentParser(description="My CLI Tool")
    subparsers = parser.add_subparsers(dest="command")

    # Subkomenda 'init'
    init_parser = subparsers.add_parser("init", help="Initialize project")
    init_parser.add_argument("--name", required=True, help="Project name")
    init_parser.add_argument(
        "--template", choices=["basic", "advanced"], help="Template"
    )

    # Subkomenda 'build'
    build_parser = subparsers.add_parser("build", help="Build project")
    build_parser.add_argument("target", help="Build target")
    build_parser.add_argument("--release", action="store_true", help="Release build")
    build_parser.add_argument("--jobs", type=int, default=4, help="Parallel jobs")

    # Konwersja
    data = argparse_to_dict(parser)

    # JSON
    print(json.dumps(data, indent=2))

    # Lub YAML (jeśli zainstalowany)
    try:
        import yaml

        print("\n--- YAML ---")
        print(yaml.dump(data, default_flow_style=False))
    except ImportError:
        pass


if __name__ == "__main__":
    main()
