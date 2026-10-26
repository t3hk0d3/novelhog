"""Custom import-linter contract: a package may import only an explicit allowlist."""

import sys
from typing import Any, cast

from grimp import ImportGraph
from importlinter import output
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck


def _is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


class AllowedImportsContract(Contract):
    """Every import made by `source_module` (and its descendants) must be allowed.

    Options:
        source_module:  the package whose imports are checked.
        allowed_modules: modules (and their descendants) that may be imported. The source
                        package itself is always allowed.
        allow_stdlib:   also allow every standard library module.
    """

    type_name = "allowed_imports"

    source_module = fields.StringField()
    allowed_modules = fields.ListField(subfield=fields.StringField())
    allow_stdlib = fields.BooleanField(required=False, default=False)

    @property
    def _source(self) -> str:
        return cast(str, self.source_module)

    def _is_allowed(self, imported: str) -> bool:
        if _is_within(imported, self._source):
            return True
        allowed_modules = cast(list[str], self.allowed_modules)
        if any(_is_within(imported, allowed) for allowed in allowed_modules):
            return True
        allow_stdlib = cast(bool, self.allow_stdlib)
        return allow_stdlib and imported.split(".")[0] in sys.stdlib_module_names

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        violations: list[dict[str, Any]] = []
        importers = sorted({self._source, *graph.find_descendants(self._source)})
        for importer in importers:
            for imported in sorted(graph.find_modules_directly_imported_by(importer)):
                if self._is_allowed(imported):
                    continue
                details = graph.get_import_details(importer=importer, imported=imported)
                lines = sorted(d["line_number"] for d in details)
                violations.append({"importer": importer, "imported": imported, "lines": lines})
        return ContractCheck(kept=not violations, metadata={"violations": violations})

    def render_broken_contract(self, check: ContractCheck) -> None:
        output.print_error(f"{self._source} may import only its allowlist:")
        output.new_line()
        for violation in check.metadata["violations"]:
            lines = ", ".join(f"l.{n}" for n in violation["lines"])
            output.print_error(
                f"-   {violation['importer']} -> {violation['imported']} ({lines})", bold=False
            )
        output.new_line()
