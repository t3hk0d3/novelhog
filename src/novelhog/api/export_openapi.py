"""Print the OpenAPI document: `python -m novelhog.api.export_openapi` (needs no database)."""

import json

from novelhog import config
from novelhog.api.app import create_app


def main() -> None:
    """Build the app without a database and print its OpenAPI JSON."""
    app = create_app(config.load({}), check_schema=False)
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
