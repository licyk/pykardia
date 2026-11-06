"""Source parse.rs scenarios through the actual YAML include and expression pipeline."""

from pykardia.include.yaml import parse
from tests.include.common import IncludeCase


class ParseTests(IncludeCase):
    """Verify source scalar semantics and expression persistence at plugin boundaries."""

    async def test_resolves_scalars_by_the_core_schema_whatever_version_the_file_declares(
        self,
    ) -> None:
        """A YAML 1.1 directive does not enable legacy scalars or merge keys."""
        await self.boot(
            "scalars.yml",
            "%YAML 1.1\n---\n- id: a\n  name: worker\n  config:\n"
            "    value: [yes, 0777, 0b101]\n    <<: { merged: true }\n",
        )
        self.assertEqual(
            self.counter.configs,
            [
                {"value": ["yes", 777, "0b101"], "<<": {"merged": True}},
            ],
        )

    async def test_writes_expressions_back_with_their_tag(self) -> None:
        """Raw expression markers persist as !!js and evaluate again after rereading."""
        include = await self.boot(
            "expression.yml",
            "- id: a\n  name: worker\n  config:\n    value: !!js \"'#' + 1\"\n",
        )
        self.assertEqual(self.counter.configs[-1], {"value": "#1"})
        await include.tree().update("a", {"config": {"value": {"__jsExpr": "'#' + 2"}}})
        await include.flush()
        text = include.filename().read_text()
        self.assertIn("    value: !!js '''#'' + 2'\n", text)
        self.assertEqual(
            parse(text)[0],
            [
                {"id": "a", "name": "worker", "config": {"value": {"__jsExpr": "'#' + 2"}}},
            ],
        )
        include.filename().write_text(text + "# touched\n")
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(self.counter.configs[-1], {"value": "#2"})
