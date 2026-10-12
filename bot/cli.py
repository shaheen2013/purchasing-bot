"""Command line: `python -m bot signup | run | report | runs`."""
from __future__ import annotations

import secrets
from enum import Enum
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Column, Table

from . import models as M
from .api import ApiError, AuthError, DemoblazeAPI
from .browser import ApiShop, BrowserSession, BrowserShop, PageCheckout, ShopError
from .config import ConfigError, load_settings, update_env_file
from .engine import Engine, new_run_id, summarize
from .ledger import Ledger
from .orders import OrdersFileError
from .report import write_reports
from .simulate import HELP as SIM_HELP, SimulationError, parse_simulations
from .ui import ConsoleUI

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Bulk Purchasing Bot demo for the Demoblaze test store.")
console = Console()
RUNS_DIR = Path("runs")


class Mode(str, Enum):
    api = "api"
    browser = "browser"


def _die(msg: str, code: int = 1) -> None:
    console.print(f"[bold red]Error:[/] {msg}")
    raise typer.Exit(code)


@app.command()
def signup(
    username: Optional[str] = typer.Option(None, help="Defaults to a random bulkbot_xxxx name."),
    password: Optional[str] = typer.Option(None, help="Defaults to a random password."),
    write_env: bool = typer.Option(True, help="Store the credentials in .env."),
    env_file: Path = typer.Option(Path(".env")),
    rps: float = typer.Option(2.0, help="Max API requests per second."),
):
    """Create a fresh Demoblaze test user through /signup."""
    username = username or f"bulkbot_{secrets.token_hex(4)}"
    password = password or secrets.token_urlsafe(12)
    api = DemoblazeAPI(rps=rps)
    try:
        api.signup(username, password)
        api.login(username, password)
    except ApiError as exc:
        _die(f"signup failed: {exc}")
    console.print(f"[green]Created and verified test user[/] [bold]{username}[/]")
    if write_env:
        update_env_file(env_file, {"DEMOBLAZE_USER": username, "DEMOBLAZE_PASS": password})
        console.print(f"Saved DEMOBLAZE_USER / DEMOBLAZE_PASS to {env_file}")
    else:
        console.print(f"Password: {password}")


@app.command()
def run(
    orders: Path = typer.Argument(Path("orders.csv"), help="CSV with columns product, qty, max_unit_price."),
    live: bool = typer.Option(False, "--live", help="Actually click Purchase. Without it this is a dry run."),
    mode: Mode = typer.Option(Mode.api, "--mode", help="api = API-first, browser only for checkout; "
                                                       "browser = everything through the UI."),
    batch_size: int = typer.Option(0, "--batch-size", help="Order lines per checkout (0 = one checkout)."),
    simulate: Optional[List[str]] = typer.Option(None, "--simulate", help="Demo switches:\n" + SIM_HELP),
    stock: Optional[Path] = typer.Option(None, "--stock", help="Simulated stock file (default: stock.yaml if present)."),
    no_stock: bool = typer.Option(False, "--no-stock", help="Ignore stock.yaml."),
    allow_partial: bool = typer.Option(False, "--allow-partial", help="Buy what is left when stock is short."),
    strict: bool = typer.Option(False, "--strict", help="Abort instead of removing unexpected cart items."),
    resume: Optional[str] = typer.Option(None, "--resume", help="Continue an earlier run from the ledger."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    slowmo: int = typer.Option(0, "--slowmo", help="Slow every browser action down by N ms."),
    rps: float = typer.Option(2.0, "--rps", help="Max API requests per second (shared public site)."),
    db: Path = typer.Option(Path("ledger.db"), "--db", help="SQLite ledger file."),
    quiet_api: bool = typer.Option(False, "--quiet-api", help="Do not log each API call."),
):
    """Plan, build, reconcile and (with --live) check out the orders."""
    try:
        settings = load_settings()
        sim = parse_simulations(simulate)
    except (ConfigError, SimulationError) as exc:
        _die(str(exc))

    ledger = Ledger(db)
    if resume:
        prev = ledger.get_run(resume)
        if prev is None:
            _die(f"no run {resume!r} in {db}")
        run_id = resume
        if prev["mode"] != mode.value:
            console.print(f"[yellow]Resuming in the run's original mode: {prev['mode']}[/]")
        mode = Mode(prev["mode"])
        if prev["live"] and not live:
            console.print("[bold yellow]This run was live, but you resumed without --live: remaining batches "
                          "will only be DRY-RUN. Add --live to continue purchasing.[/]")
    else:
        run_id = new_run_id()
        if stock is None and not no_stock and Path("stock.yaml").exists():
            stock = Path("stock.yaml")
        if no_stock:
            stock = None
        if not orders.exists():
            _die(f"{orders} not found")

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.joinpath("LAST_RUN").write_text(run_id, encoding="utf-8")

    banner = "[bold white on red] LIVE [/] Purchase WILL be clicked (test store, fake card)" if live else \
        "[bold black on yellow] DRY RUN [/] stops before Purchase (add --live to buy)"
    console.print(Panel(f"{banner}\nrun {run_id}  mode {mode.value}  ledger {db}  throttle {rps:g} req/s"
                        + ("  [resume]" if resume else ""), border_style="red" if live else "yellow"))

    ui = ConsoleUI(console, show_api=not quiet_api)
    session = BrowserSession(settings.web_base, run_dir / "screens", headed=headed, slowmo=slowmo,
                             log=lambda m: ui.log(m, "dim"))
    checkout = PageCheckout(session)
    exit_code = 0
    try:
        if mode == Mode.api:
            api = DemoblazeAPI(settings.api_base, rps=rps, on_call=ui.api)
            token = api.login(settings.username, settings.password)
            ui.log(f"Logged in via API as {settings.username}", "green")
            session.set_token(token)  # the browser reuses the API session for checkout only
            shop = ApiShop(api, token, settings.username)
        else:
            shop = BrowserShop(session, checkout, settings.username, settings.password)
            ui.log("Browser mode: logging in through the UI login modal")
            shop.login()
            ui.log(f"Logged in via UI as {settings.username}", "green")

        engine = Engine(ledger, shop, checkout, settings.checkout, ui, live=live, strict=strict, sim=sim)
        ui.attach(ledger, run_id, "LIVE" if live else "DRY RUN")
        ui.start()
        if not resume:
            engine.plan(run_id, orders, mode=mode.value, batch_size=batch_size,
                        allow_partial=allow_partial, stock_path=stock)
        else:
            ui.log(f"Resuming {run_id} from the ledger (the plan is not recomputed)", "cyan")
        status = engine.execute(run_id)
        exit_code = 0 if status in (M.R_COMPLETED, M.R_DRY_RUN_COMPLETE) else 2
    except KeyboardInterrupt:
        exit_code = 130
        if ledger.get_run(run_id) and ledger.get_run(run_id)["status"] != M.R_INTERRUPTED:
            ledger.set_run_status(run_id, M.R_INTERRUPTED, "Ctrl+C")
    except (AuthError, ShopError) as exc:
        ui.stop()
        _die(f"login failed: {exc}")
    except (ApiError, OrdersFileError) as exc:
        ui.stop()
        _die(str(exc))
    finally:
        ui.stop()
        session.close()

    if ledger.get_run(run_id):
        console.print()
        _print_summary(ledger, run_id)
        csv_path, html_path = write_reports(ledger, run_id, RUNS_DIR)
        console.print(f"Reports: {csv_path}  {html_path}")
        if exit_code == 130:
            console.print(f"[bold yellow]Interrupted. Resume with:[/] python -m bot run --resume {run_id}"
                          + (" --live" if live else ""))
    raise typer.Exit(exit_code)


def _print_summary(ledger: Ledger, run_id: str) -> None:
    s = summarize(ledger, run_id)
    run = ledger.get_run(run_id)
    t = Table(title=f"Run {run_id}: {run['status']}", show_header=False)
    for k, v in (("Ordered units", s["ordered_units"]), ("Confirmed batches", f"{s['batches_confirmed']}/{s['batches']}"),
                 ("Skipped lines", f"{s['skipped']} (rejected {s['rejected']})"), ("Failed/uncertain lines", s["failed"]),
                 ("Spend (confirmed)", f"${s['spend']:,}"), ("Planned spend", f"${s['planned_spend']:,}"),
                 ("Order ids", ", ".join(s["orders"]) or "-")):
        t.add_row(k, str(v))
    console.print(t)


@app.command()
def report(run_id: str, db: Path = typer.Option(Path("ledger.db"), "--db")):
    """Re-generate runs/<run_id>/report.csv and report.html from the ledger."""
    ledger = Ledger(db)
    if not ledger.get_run(run_id):
        _die(f"no run {run_id!r}")
    _print_summary(ledger, run_id)
    for p in write_reports(ledger, run_id, RUNS_DIR):
        console.print(str(p))


@app.command()
def runs(db: Path = typer.Option(Path("ledger.db"), "--db"), limit: int = 20):
    """List recent runs in the ledger."""
    ledger = Ledger(db)
    t = Table(Column("run_id", no_wrap=True, style="cyan"), "created (UTC)", "mode", "live", "status", "batches")
    for r in ledger.list_runs(limit):
        bs = ledger.batches(r["run_id"])
        done = sum(b["state"] in M.DONE_OK for b in bs)
        t.add_row(r["run_id"], r["created_at"][:19].replace("T", " "), r["mode"], "yes" if r["live"] else "no", r["status"], f"{done}/{len(bs)}")
    console.print(t)
