"""Google Drive sandbox management for WorkflowWeaver (Phase 6.1).

    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox check                       # READ-ONLY probe
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox create-folder --confirm     # one real write
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox cleanup --confirm           # trash test runs

create-folder creates ONE folder, "WorkflowWeaver Sandbox", in the root of your My Drive with
the private app property workflowweaver_sandbox=true, and prints its id for
GOOGLE_DRIVE_DESTINATIONS. Under the drive.file scope this app can only ever see that folder
and what it creates inside it.

cleanup moves to trash (recoverable for 30 days) only items INSIDE the sandbox folder that
carry this app's workflowweaver_run property - i.e. run folders and reports created by
WorkflowWeaver. It never deletes permanently and never touches the sandbox folder itself.

upload-source (Phase 7.2) uploads ONE Markdown source document into the sandbox folder: the
built-in synthetic Customer Rewards Platform implementation plan, or --file (UTF-8 .md, at most
200 KB):
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox upload-source --confirm
    .\\.venv\\Scripts\\python.exe -m scripts.google_drive_sandbox upload-source --file plan.md --confirm
Under drive.file this app can only read files it created, so a document uploaded through the
Drive web UI is invisible to it: use this command. The file is marked as a source
(workflowweaver_source=true), not as a run output, so cleanup never trashes it.

Write commands refuse to run without --confirm. Tokens are never printed.
"""

import argparse
import sys

from app.agents.execution.tools.integrations import _drive_client
from app.core.config import Settings
from app.core.log_redaction import install_secret_redaction
from app.integrations.config import load_integration_config
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.google.drive import FOLDER_MIME, RUN_PROPERTY, SANDBOX_PROPERTY
from app.integrations.readiness import probe_google_drive

SANDBOX_NAME = "WorkflowWeaver Sandbox"


def _config(settings: Settings):
    config = load_integration_config(settings)
    if config.drive_rest is None:
        raise IntegrationConfigError("Set GOOGLE_DRIVE_TRANSPORT=rest and the GOOGLE_OAUTH_CLIENT_* settings first.")
    return config


def check(settings: Settings, *, http=None) -> int:
    config = _config(settings)
    if not settings.smoke_test_drive_folder or settings.smoke_test_drive_folder not in config.drive_folders.targets:
        print("SMOKE_TEST_DRIVE_FOLDER must be an alias listed in GOOGLE_DRIVE_DESTINATIONS.")
        return 2
    result = probe_google_drive(settings, config, http)
    for c in result.checks:
        print(f"[{c['status']}] {c['name']}{': ' + c['detail'] if c['detail'] else ''}")
    return 0 if result.passed else 1


def create_folder(settings: Settings, *, confirm: bool, http=None) -> int:
    if not confirm:
        print("This creates ONE real folder in your Google Drive. Re-run with --confirm.")
        return 2
    client = _drive_client(_config(settings), http)
    try:
        meta = client.create_folder(SANDBOX_NAME, "root", {SANDBOX_PROPERTY: "true"})
    finally:
        client.close()
    print(f"Created '{SANDBOX_NAME}'. Add it to .env:")
    print(f'  GOOGLE_DRIVE_DESTINATIONS={{"sandbox": "{meta["id"]}"}}')
    print("  SMOKE_TEST_DRIVE_FOLDER=sandbox")
    return 0


def cleanup(settings: Settings, *, confirm: bool, http=None) -> int:
    config = _config(settings)
    alias = settings.smoke_test_drive_folder
    if not alias or alias not in config.drive_folders.targets:
        print("SMOKE_TEST_DRIVE_FOLDER must be an alias listed in GOOGLE_DRIVE_DESTINATIONS.")
        return 2
    client = _drive_client(config, http)
    try:
        sandbox = client.get(config.drive_folders.targets[alias])
        if sandbox.get("mimeType") != FOLDER_MIME or (sandbox.get("appProperties") or {}).get(SANDBOX_PROPERTY) != "true":
            print("The configured folder is not the WorkflowWeaver sandbox folder; nothing was changed.")
            return 1
        items = [f for f in client.list_children(sandbox["id"]) if (f.get("appProperties") or {}).get(RUN_PROPERTY)]
        if not confirm:
            print(f"{len(items)} WorkflowWeaver item(s) would be moved to trash. Re-run with --confirm.")
            return 2
        for item in items:
            client.trash(item["id"])  # trashing a folder trashes its contents too
        print(f"Moved {len(items)} WorkflowWeaver item(s) to Drive trash (recoverable for 30 days).")
        return 0
    finally:
        client.close()


def upload_source(settings: Settings, *, confirm: bool, path: str | None = None, http=None) -> int:
    from pathlib import Path

    from app.agents.orchestration import MAX_SOURCE_BYTES
    from app.agents.source_documents import CUSTOMER_REWARDS_PLAN, SOURCE_NAME

    if path:
        file = Path(path)
        if file.suffix.lower() != ".md" or not file.is_file():
            print("--file must be an existing .md file.")
            return 2
        content, name = file.read_bytes(), file.name
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            print("--file must be UTF-8 text.")
            return 2
    else:
        content, name = CUSTOMER_REWARDS_PLAN.encode("utf-8"), SOURCE_NAME
    if len(content) > MAX_SOURCE_BYTES:
        print(f"The document exceeds {MAX_SOURCE_BYTES} bytes.")
        return 2
    if not confirm:
        print(f"This uploads ONE real file ('{name}', {len(content)} bytes) into the Drive sandbox folder. "
              "Re-run with --confirm.")
        return 2
    config = _config(settings)
    alias = settings.smoke_test_drive_folder
    if not alias or alias not in config.drive_folders.targets:
        print("SMOKE_TEST_DRIVE_FOLDER must be an alias listed in GOOGLE_DRIVE_DESTINATIONS.")
        return 2
    client = _drive_client(config, http)
    try:
        sandbox = client.get(config.drive_folders.targets[alias])
        if sandbox.get("mimeType") != FOLDER_MIME or (sandbox.get("appProperties") or {}).get(SANDBOX_PROPERTY) != "true":
            print("The configured folder is not the WorkflowWeaver sandbox folder; nothing was uploaded.")
            return 1
        meta = client.upload_text(name, content, "text/markdown", sandbox["id"], {"workflowweaver_source": "true"})
    finally:
        client.close()
    print(f"Uploaded '{name}' to the sandbox folder (file id {meta.get('id')}). Select it in the "
          "Google Drive + Notion + Teams workflow.")
    return 0


def main(argv: list[str] | None = None) -> int:
    install_secret_redaction()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "create-folder", "cleanup", "upload-source"])
    parser.add_argument("--file", help="upload-source: a local UTF-8 .md file (default: the synthetic sample plan)")
    parser.add_argument("--confirm", action="store_true", help="required for commands that write to Drive")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if args.command == "check":
            return check(settings)
        if args.command == "create-folder":
            return create_folder(settings, confirm=args.confirm)
        if args.command == "upload-source":
            return upload_source(settings, confirm=args.confirm, path=args.file)
        return cleanup(settings, confirm=args.confirm)
    except IntegrationConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    except IntegrationError as exc:
        print(f"Google Drive error: {exc.code}: {exc.message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
