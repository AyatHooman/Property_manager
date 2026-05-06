"""
CLI interface — entry point for the Property Manager application.
Usage: python -m src.cli [COMMAND] [OPTIONS]
"""
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from typing import Optional

from src.api_client import DomainAPIClient
from src import database as db

app = typer.Typer(
    name="property-manager",
    help="🏠 Australian Property Manager — powered by the Domain API",
    add_completion=False,
)
console = Console()


def get_client() -> DomainAPIClient:
    try:
        return DomainAPIClient()
    except ValueError as e:
        console.print(f"[bold red]❌ Setup Error:[/bold red] {e}")
        raise typer.Exit(1)


# ── Search ─────────────────────────────────────────────────────────────────────

@app.command()
def search(
    suburb: str = typer.Argument(..., help="Suburb name (e.g. Surry Hills)"),
    state: str = typer.Argument(..., help="State abbreviation (e.g. NSW)"),
    listing_type: str = typer.Option("Sale", "--type", "-t", help="Sale or Rent"),
    min_price: Optional[int] = typer.Option(None, "--min-price", help="Minimum price"),
    max_price: Optional[int] = typer.Option(None, "--max-price", help="Maximum price"),
    min_beds: Optional[int] = typer.Option(None, "--min-beds", help="Minimum bedrooms"),
    max_beds: Optional[int] = typer.Option(None, "--max-beds", help="Maximum bedrooms"),
    page_size: int = typer.Option(20, "--limit", "-n", help="Number of results"),
):
    """Search for property listings in a suburb."""
    db.init_db()
    client = get_client()

    console.print(
        Panel(
            f"[bold cyan]Searching {listing_type} listings in [yellow]{suburb}, {state}[/yellow][/bold cyan]",
            expand=False,
        )
    )

    try:
        listings = client.search_listings(
            suburb=suburb,
            state=state.upper(),
            listing_type=listing_type,
            min_price=min_price,
            max_price=max_price,
            min_beds=min_beds,
            max_beds=max_beds,
            page_size=page_size,
        )
    except Exception as e:
        console.print(f"[red]API error: {e}[/red]")
        raise typer.Exit(1)

    db.log_search(f"{listing_type} in {suburb}, {state}")

    if not listings:
        console.print("[yellow]No listings found.[/yellow]")
        return

    table = Table(box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Address", style="bold")
    table.add_column("Price", style="green")
    table.add_column("Type")
    table.add_column("Beds", justify="center")
    table.add_column("Baths", justify="center")
    table.add_column("Cars", justify="center")

    for listing in listings:
        table.add_row(
            str(listing.id),
            listing.address or "—",
            listing.price or "—",
            listing.property_type or "—",
            str(listing.bedrooms or "—"),
            str(listing.bathrooms or "—"),
            str(listing.carspaces or "—"),
        )

    console.print(table)
    console.print(f"[dim]Found {len(listings)} listings. Use [bold]save <ID>[/bold] to bookmark one.[/dim]")


# ── Suburb Performance ─────────────────────────────────────────────────────────

@app.command()
def suburb(
    suburb_name: str = typer.Argument(..., help="Suburb name"),
    state: str = typer.Argument(..., help="State abbreviation (e.g. NSW)"),
    postcode: str = typer.Argument(..., help="Postcode"),
    property_category: str = typer.Option("house", "--category", "-c", help="house, unit, or land"),
    bedrooms: int = typer.Option(3, "--beds", "-b", help="Number of bedrooms"),
):
    """Show suburb performance statistics."""
    db.init_db()
    client = get_client()

    try:
        profile = client.get_suburb_performance(
            suburb=suburb_name,
            state=state.upper(),
            postcode=postcode,
            property_category=property_category,
            bedrooms=bedrooms,
        )
    except Exception as e:
        console.print(f"[red]API error: {e}[/red]")
        raise typer.Exit(1)

    def fmt_price(v):
        return f"${v:,.0f}" if v else "N/A"

    table = Table(title=f"📊 {suburb_name}, {state} — {property_category.capitalize()} ({bedrooms} bed)", box=box.SIMPLE_HEAD)
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("Median Sale Price", fmt_price(profile.median_sale_price))
    table.add_row("Median Rent Price", fmt_price(profile.median_rent_price))
    table.add_row("Days on Market", str(profile.days_on_market or "N/A"))
    table.add_row("Properties Sold", str(profile.properties_sold or "N/A"))
    table.add_row("Auction Clearance", str(profile.auction_clearance_rate or "N/A"))

    console.print(table)


# ── Sales Results ──────────────────────────────────────────────────────────────

@app.command()
def sales(
    suburb_name: str = typer.Argument(..., help="Suburb name"),
    state: str = typer.Argument(..., help="State abbreviation"),
    postcode: str = typer.Argument(..., help="Postcode"),
):
    """Show recent sales results for a suburb."""
    db.init_db()
    client = get_client()

    try:
        results = client.get_sales_results(suburb_name, state.upper(), postcode)
    except Exception as e:
        console.print(f"[red]API error: {e}[/red]")
        raise typer.Exit(1)

    if not results:
        console.print("[yellow]No sales results found.[/yellow]")
        return

    table = Table(title=f"🏷️  Recent Sales — {suburb_name}, {state}", box=box.ROUNDED)
    table.add_column("Address", style="bold")
    table.add_column("Price", style="green")
    table.add_column("Sold Date")
    table.add_column("Type")
    table.add_column("Beds", justify="center")

    for r in results:
        table.add_row(
            r.address or "—",
            f"${r.price:,.0f}" if r.price else "Undisclosed",
            r.sold_date or "—",
            r.property_type or "—",
            str(r.bedrooms or "—"),
        )

    console.print(table)


# ── Save / View Saved ──────────────────────────────────────────────────────────

@app.command()
def save(listing_id: int = typer.Argument(..., help="Listing ID to save")):
    """Save a listing by ID (fetch details and bookmark it)."""
    db.init_db()
    client = get_client()

    try:
        listing = client.get_listing(listing_id)
    except Exception as e:
        console.print(f"[red]Could not fetch listing {listing_id}: {e}[/red]")
        raise typer.Exit(1)

    saved = db.save_listing(listing)
    if saved:
        console.print(f"[green]✅ Saved:[/green] {listing.address} ({listing.price})")
    else:
        console.print(f"[yellow]Already saved listing {listing_id}.[/yellow]")


@app.command()
def saved():
    """View all saved listings."""
    db.init_db()
    listings = db.get_saved_listings()

    if not listings:
        console.print("[yellow]No saved listings yet. Use [bold]save <ID>[/bold] to bookmark one.[/yellow]")
        return

    table = Table(title="⭐ Saved Listings", box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("Address", style="bold")
    table.add_column("Suburb")
    table.add_column("Price", style="green")
    table.add_column("Beds", justify="center")
    table.add_column("URL", style="blue")

    for l in listings:
        table.add_row(
            str(l["listing_id"]),
            l["address"] or "—",
            f"{l['suburb']}, {l['state']}",
            l["price"] or "—",
            str(l["bedrooms"] or "—"),
            l["url"] or "—",
        )

    console.print(table)


@app.command()
def unsave(listing_id: int = typer.Argument(..., help="Listing ID to remove")):
    """Remove a listing from saved bookmarks."""
    db.init_db()
    removed = db.remove_saved_listing(listing_id)
    if removed:
        console.print(f"[green]Removed listing {listing_id} from saved.[/green]")
    else:
        console.print(f"[yellow]Listing {listing_id} not found in saved.[/yellow]")


# ── History ────────────────────────────────────────────────────────────────────

@app.command()
def history():
    """Show recent search history."""
    db.init_db()
    searches = db.get_search_history()
    if not searches:
        console.print("[yellow]No search history yet.[/yellow]")
        return
    console.print("[bold]Recent Searches:[/bold]")
    for s in searches:
        console.print(f"  [cyan]•[/cyan] {s}")


# ── Suggest ────────────────────────────────────────────────────────────────────

@app.command()
def suggest(query: str = typer.Argument(..., help="Suburb name to look up")):
    """Autocomplete a suburb name using the Domain API."""
    client = get_client()
    try:
        results = client.suggest_suburbs(query)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    if not results:
        console.print("[yellow]No suggestions found.[/yellow]")
        return

    for item in results[:10]:
        console.print(f"  [cyan]•[/cyan] {item.get('suggestion', item)}")


if __name__ == "__main__":
    app()
