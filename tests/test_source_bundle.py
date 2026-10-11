from pathlib import Path

from decinav.source_bundle import (
    MAX_SOURCE_BUNDLE_BYTES,
    MAX_SOURCE_FILE_BYTES,
    build_baseline_bundle,
    build_ingest_bundle,
)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_baseline_bundle_includes_only_safe_structural_files(tmp_path: Path) -> None:
    write(tmp_path / "package.json", '{"name":"platform"}\n')
    write(tmp_path / "docker-compose.override.yml", "services: {}\n")
    write(tmp_path / "services" / "api" / "pom.xml", "<project/>\n")
    write(tmp_path / "infra" / "main.tf", 'resource "aws_s3_bucket" "assets" {}\n')
    write(
        tmp_path / "deploy" / "deployment.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
    )
    write(
        tmp_path / "deploy" / "secret.yaml",
        "apiVersion: v1\nkind: Secret\nstringData:\n  password: production\n",
    )
    write(tmp_path / "src" / "app.py", "print('application source')\n")
    write(tmp_path / ".env.production", "API_KEY=do-not-send\n")
    write(tmp_path / "package-lock.json", '{"lockfileVersion":3}\n')
    write(tmp_path / "config.yaml", "feature: enabled\n")
    write(tmp_path / "node_modules" / "dependency" / "package.json", "{}\n")
    write(tmp_path / "docs" / "adr" / "0001-old.md", "not source context\n")

    bundle = build_baseline_bundle(tmp_path)

    paths = {source.relative_path for source in bundle.files}
    assert paths == {
        "deploy/deployment.yaml",
        "docker-compose.override.yml",
        "infra/main.tf",
        "package.json",
        "services/api/pom.xml",
    }
    assert "application source" not in bundle.text
    assert "do-not-send" not in bundle.text
    assert "production" not in bundle.text
    assert any("Secret kind" in warning.reason for warning in bundle.warnings)


def test_baseline_bundle_enforces_per_file_and_total_limits(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "x" * (MAX_SOURCE_FILE_BYTES + 1))
    for index in range(10):
        write(tmp_path / f"service-{index}" / "package.json", "x" * (120 * 1024))

    bundle = build_baseline_bundle(tmp_path)

    assert bundle.size_bytes <= MAX_SOURCE_BUNDLE_BYTES
    assert len(bundle.files) < 10
    assert any("exceeds" in warning.reason for warning in bundle.warnings)
    assert any("1 MiB" in warning.reason for warning in bundle.warnings)


def test_ingest_bundle_reads_only_supported_utf8_documents(tmp_path: Path) -> None:
    write(tmp_path / "prd.md", "# Product decision\n")
    write(tmp_path / "notes.txt", "Use a queue.\n")
    write(tmp_path / "architecture.rst", "Architecture\n============\n")
    write(tmp_path / "decision.adoc", "= Decision\n")
    write(tmp_path / "ignored.pdf", "not actually a PDF\n")
    write(tmp_path / "src" / "notes.py", "decision = True\n")
    write(tmp_path / "node_modules" / "dependency.md", "ignore\n")
    binary = tmp_path / "broken.md"
    binary.write_bytes(b"\xff\xfe")

    bundle = build_ingest_bundle(tmp_path)

    assert {source.relative_path for source in bundle.files} == {
        "architecture.rst",
        "decision.adoc",
        "notes.txt",
        "prd.md",
    }
    assert any("non-UTF-8" in warning.reason for warning in bundle.warnings)


def test_ingest_excludes_generated_adr_directory_when_docs_is_root(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    adr_dir = docs / "adr"
    adr_dir.mkdir(parents=True)
    write(docs / "feature.md", "Feature decision.\n")
    write(
        adr_dir / "0001-generated.md",
        "Generated ADR must not become source context.\n",
    )

    bundle = build_ingest_bundle(docs)

    assert [source.relative_path for source in bundle.files] == ["feature.md"]
    assert "Generated ADR must not become source context" not in bundle.text
