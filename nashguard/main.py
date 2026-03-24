"""
NashGuard CLI — Interactive trading terminal with MoA debate visualisation.

Usage:
    python -m nashguard
    python -m nashguard --demo          # Force demo mode
    python -m nashguard --live          # Live trading (requires credentials)
    python -m nashguard --no-color      # Disable rich formatting
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

from .config import Config
from .models import NashGuardResult
from .orchestrator import NashGuardOrchestrator

console = Console()

BANNER = r"""
  _   _           _     _____                     _
 | \ | |         | |   / ____|                   | |
 |  \| | __ _ ___| |__| |  __ _   _  __ _ _ __ __| |
 | . ` |/ _` / __| '_ \ | |_ | | | |/ _` | '__/ _` |
 | |\  | (_| \__ \ | | | |__| | |_| | (_| | | | (_| |
 |_| \_|\__,_|___/_| |_|\_____|\_,_|\__,_|_|  \__,_|

 MoA Multi-Agent Debate  ×  DRB Deterministic Risk Boundary
 Powered by OKX Agent Trade Kit  ×  litellm (Claude / GPT / Gemini / Ollama ...)
"""

STAGE_ICONS = {
    "snapshot": "[cyan]◉[/cyan]",
    "alpha":    "[yellow]α[/yellow]",
    "risk":     "[red]⚠[/red]",
    "judge":    "[blue]⚖[/blue]",
    "drb":      "[magenta]🔐[/magenta]",
    "execute":  "[green]▶[/green]",
}


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def make_progress_callback(live_panel_ref: list):
    """Return a callback that prints stage updates to the console."""
    def callback(stage: str, detail: str) -> None:
        icon = STAGE_ICONS.get(stage, "•")
        console.print(f"  {icon} {detail}")
    return callback


def render_result(result: NashGuardResult) -> None:
    """Pretty-print the full NashGuard result."""
    console.print()
    console.print(Rule("[bold]NashGuard Decision[/bold]", style="dim"))

    # ── Overall verdict ──
    if result.rejected:
        verdict_text = Text("✗  BLOCKED", style="bold red")
        reason = result.rejection_reason or "Unknown reason"
        console.print(Panel(
            f"[red]{reason}[/red]",
            title=verdict_text,
            border_style="red",
        ))
    else:
        verdict_text = Text("✓  APPROVED & EXECUTED", style="bold green")
        user_msg = result.judge_decision.user_message if result.judge_decision else ""
        console.print(Panel(
            f"[green]{user_msg}[/green]",
            title=verdict_text,
            border_style="green",
        ))

    # ── Debate summary ──
    if result.alpha_proposal or result.risk_assessment:
        debate_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        debate_table.add_column("Field", style="dim", width=22)
        debate_table.add_column("Value")

        debate_table.add_row("Debate rounds", str(result.debate_rounds))
        if result.alpha_proposal:
            debate_table.add_row("Final strategy", result.alpha_proposal.strategy)
            debate_table.add_row(
                "Expected return",
                f"{result.alpha_proposal.expected_return_pct:+.1f}%",
            )
        if result.risk_assessment:
            risk_color = "green" if result.risk_assessment.risk_score <= 5 else "red"
            debate_table.add_row(
                "Risk score",
                f"[{risk_color}]{result.risk_assessment.risk_score:.1f}/10[/{risk_color}]",
            )
        if result.judge_decision:
            conf_color = "green" if result.judge_decision.confidence >= 0.7 else "yellow"
            debate_table.add_row(
                "Judge confidence",
                f"[{conf_color}]{result.judge_decision.confidence:.0%}[/{conf_color}]",
            )

        console.print(Panel(debate_table, title="[cyan]MoA Debate Summary[/cyan]", border_style="cyan"))

    # ── DRB Risk Metrics ──
    if result.drb_result:
        drb = result.drb_result
        drb_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        drb_table.add_column("Metric", style="dim", width=26)
        drb_table.add_column("Value")

        dd_color = "green" if drb.max_drawdown_pct < 10 else ("yellow" if drb.max_drawdown_pct < 20 else "red")
        drb_table.add_row("Max Drawdown (MC 99%)", f"[{dd_color}]{drb.max_drawdown_pct:.1f}%[/{dd_color}]")
        drb_table.add_row("VaR 95%", f"{drb.var_95_pct:.1f}%")
        drb_table.add_row("CVaR (Expected Shortfall)", f"{drb.expected_shortfall_pct:.1f}%")
        if drb.liquidation_price:
            drb_table.add_row("Estimated Liq. Price", f"${drb.liquidation_price:,.0f}")
        drb_table.add_row(
            "Monte Carlo sims",
            f"{drb.details.get('n_simulations', 0):,}",
        )
        drb_table.add_row(
            "DRB Signature",
            f"[dim]{drb.signature_hex[:32]}...[/dim]",
        )
        status = "[green]VERIFIED ✓[/green]" if drb.approved else "[red]REJECTED ✗[/red]"
        drb_table.add_row("Sandbox verdict", status)

        console.print(
            Panel(drb_table, title="[magenta]DRB Sandbox Results[/magenta]", border_style="magenta")
        )

    # ── Execution results ──
    if result.execution_results:
        exec_table = Table(box=box.SIMPLE, padding=(0, 1))
        exec_table.add_column("Tool", style="cyan")
        exec_table.add_column("Status")
        exec_table.add_column("Details", style="dim")

        for r in result.execution_results:
            status = "[green]OK[/green]" if r.get("success") else "[red]FAILED[/red]"
            details = str(r.get("result", r.get("error", "")))[:80]
            exec_table.add_row(r["tool"], status, details)

        console.print(
            Panel(exec_table, title="[green]Execution Log[/green]", border_style="green")
        )

    # ── Audit trail ──
    if result.audit_log:
        audit_text = "\n".join(f"[dim]{line}[/dim]" for line in result.audit_log)
        console.print(
            Panel(audit_text, title="[dim]Full Audit Trail[/dim]", border_style="dim")
        )


async def run_interactive(config: Config) -> None:
    """Interactive REPL for NashGuard."""
    console.print(BANNER, style="bold cyan")
    mode_label = "[yellow]DEMO MODE[/yellow]" if config.demo_mode else "[bold red]LIVE MODE[/bold red]"
    console.print(f"  Status: {mode_label}")

    if not config.has_llm_key:
        console.print(
            "[red]ERROR: No LLM API key found.[/red]\n"
            "[dim]Set one of: ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / "
            "MISTRAL_API_KEY / DEEPSEEK_API_KEY\n"
            "Or use a local Ollama model (no key needed): "
            "NASHGUARD_ALPHA_MODEL=ollama/llama3.2[/dim]"
        )
        sys.exit(1)

    if not config.has_okx_credentials:
        console.print(
            "[yellow]Warning: No OKX credentials found. Using mock account data.[/yellow]"
        )

    console.print(
        Panel(
            "[dim]Type a trading request in plain language.\n"
            "Examples:\n"
            "  • 帮我全仓做多 BTC，用最高杠杆，立刻执行！\n"
            "  • Buy $1000 of ETH spot\n"
            "  • I want to hedge my BTC with a put option\n"
            "  • exit / quit — to leave[/dim]",
            title="[bold]NashGuard Interactive Terminal[/bold]",
            border_style="blue",
        )
    )

    orchestrator = NashGuardOrchestrator(
        config,
        progress_callback=make_progress_callback([]),
    )

    while True:
        console.print()
        try:
            user_input = console.input("[bold cyan]nashguard>[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        console.print(Rule(f"[dim]Processing: {user_input[:60]}[/dim]", style="dim"))
        start = time.monotonic()

        try:
            result = await orchestrator.run(user_input)
        except Exception as e:
            console.print(f"[red]Orchestrator error: {e}[/red]")
            logging.exception("Orchestrator error")
            continue

        elapsed = time.monotonic() - start
        console.print(f"  [dim]Completed in {elapsed:.1f}s[/dim]")
        render_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="NashGuard — AI trading with MoA + DRB safety")
    parser.add_argument("--demo", action="store_true", help="Force demo/simulated trading mode")
    parser.add_argument("--live", action="store_true", help="Enable live trading (disables demo)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    config = Config()
    if args.demo:
        config.demo_mode = True
    if args.live:
        config.demo_mode = False

    asyncio.run(run_interactive(config))


if __name__ == "__main__":
    main()
