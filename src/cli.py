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

from src import scraper
from src import database as db

app = typer.Typer(
    name="property-manager",
    help="🏠 Australian Property Manager — powered by domain.com.au",
    add_completion=False,
)
console = Console()


# ── Search ─────────────────────────────────────────────────────────────────────

@app.command()
def search(
    suburb: str = typer.Argument(..., help="Suburb name (e.g. Richmond)"),
    state: str = typer.Argument(..., help="State abbreviation (e.g. VIC)"),
    postcode: str = typer.Option("", "--postcode", "-p", help="Postcode (optional, improves accuracy)"),
    listing_type: str = typer.Option("Sale", "--type", "-t", help="Sale or Rent"),
    min_price: Optional[int] = typer.Option(None, "--min-price", help="Minimum price"),
    max_price: Optional[int] = typer.Option(None, "--max-price", help="Maximum price"),
    min_beds: Optional[int] = typer.Option(None, "--min-beds", help="Minimum bedrooms"),
    page: int = typer.Option(1, "--page", help="Page number"),
):
    """Search for property listings on domain.com.au."""
    db.init_db()

    console.print(
        Panel(
            f"[bold cyan]Searching [yellow]{listing_type}[/yellow] listings in [yellow]{suburb}, {state}[/yellow] — domain.com.au[/bold cyan]",
            expand=False,
        )
    )

    with console.status("[bold green]Fetching listings..."):
        try:
            listings = scraper.search_listings(
                suburb=suburb,
                state=state.upper(),
                postcode=postcode,
                listing_type=listing_type,
                min_price=min_price,
                max_price=max_price,
                min_beds=min_beds,
                page=page,
            )
        except Exception as e:
            console.print(f"[red]Scrape error: {e}[/red]")
            raise typer.Exit(1)

    db.log_search(f"{listing_type} in {suburb}, {state}")

    if not listings:
        console.print("[yellow]No listings found. Try adding a postcode with --postcode[/yellow]")
        return

    table = Table(box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="dim", width=12)
    table.add_column("Address", style="bold", min_width=25)
    table.add_column("Price", style="green", min_width=15)
    table.add_column("Type", min_width=10)
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
    console.print(
        f"[dim]Found {len(listings)} listings. "
        f"Use [bold]--page 2[/bold] for more results.[/dim]"
    )


# ── Nearby Sales ───────────────────────────────────────────────────────────────

@app.command()
def nearby_sales(
    listing_id: int = typer.Argument(..., help="Listing ID to search around (from search results)"),
    radius: float = typer.Option(5.0, "--radius", "-r", help="Search radius in km (default 5)"),
    months: int = typer.Option(6, "--months", "-m", help="How many months back to search (default 6)"),
    pages: int = typer.Option(3, "--pages", help="Number of result pages to fetch (default 3)"),
    url: Optional[str] = typer.Option(None, "--url", help="Full domain.com.au listing URL (e.g. https://www.domain.com.au/18-765-malvern-road-toorak-vic-3142-2020795806)"),
):
    """Find sold properties within a radius of a given listing, filtered by timeframe."""
    db.init_db()

    console.print(
        Panel(
            f"[bold cyan]Finding sold properties within [yellow]{radius}km[/yellow] "
            f"of listing [yellow]{listing_id}[/yellow] — last [yellow]{months} months[/yellow][/bold cyan]",
            expand=False,
        )
    )

    with console.status("[bold green]Locating property..."):
        location = scraper.get_listing_location(listing_id, listing_url=url)

    if not location:
        console.print(f"[red]Could not find lat/lng for listing {listing_id}.[/red]")
        console.print("[dim]Tip: provide the full URL with [bold]--url https://www.domain.com.au/address-suburb-state-postcode-{listing_id}[/bold][/dim]")
        raise typer.Exit(1)

    lat, lng, address, suburb_val, state_val, postcode_val = location
    console.print(f"[green]📍 Reference property:[/green] {address}")
    console.print(f"[dim]Coordinates: {lat:.5f}, {lng:.5f} — searching {radius}km radius in {suburb_val}[/dim]\n")

    with console.status(f"[bold green]Scraping sold listings ({pages} pages)..."):
        try:
            results = scraper.get_nearby_sales(
                lat, lng,
                radius_km=radius,
                months=months,
                pages=pages,
                suburb=suburb_val,
                state=state_val,
                postcode=postcode_val,
            )
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)

    if not results:
        console.print(f"[yellow]No sold properties found within {radius}km in the last {months} months.[/yellow]")
        return

    table = Table(
        title=f"🏷️  {len(results)} Sold Properties within {radius}km — Last {months} months",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Address", style="bold", min_width=28)
    table.add_column("Price", style="green", min_width=14)
    table.add_column("Sold Date", min_width=11)
    table.add_column("Type", min_width=10)
    table.add_column("Beds", justify="center")
    table.add_column("Dist (km)", justify="right", style="cyan")

    for r in results:
        table.add_row(
            r.address or "—",
            r.price_display or (f"${r.price:,.0f}" if r.price else "Undisclosed"),
            r.sold_date[:10] if r.sold_date else "—",
            r.property_type or "—",
            str(r.bedrooms or "—"),
            f"{r.distance_km:.2f}" if r.distance_km is not None else "—",
        )

    console.print(table)
    console.print(
        f"[dim]Tip: use [bold]--radius 2[/bold] for tighter area or [bold]--months 3[/bold] for recent only.[/dim]"
    )


# ── Sales Results ──────────────────────────────────────────────────────────────

@app.command()
def sales(
    suburb_name: str = typer.Argument(..., help="Suburb name"),
    state: str = typer.Argument(..., help="State abbreviation"),
    postcode: str = typer.Option("", "--postcode", "-p", help="Postcode (optional)"),
):
    """Show recent sold properties for a suburb."""
    db.init_db()

    with console.status("[bold green]Fetching sold listings..."):
        try:
            results = scraper.get_sales_results(suburb_name, state.upper(), postcode)
        except Exception as e:
            console.print(f"[red]Scrape error: {e}[/red]")
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
def saved():
    """View all saved listings."""
    db.init_db()
    listings = db.get_saved_listings()

    if not listings:
        console.print("[yellow]No saved listings yet.[/yellow]")
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
def suggest(query: str = typer.Argument(..., help="Suburb name to autocomplete")):
    """Autocomplete a suburb name."""
    with console.status("Looking up suburbs..."):
        results = scraper.suggest_suburbs(query)

    if not results:
        console.print("[yellow]No suggestions found.[/yellow]")
        return

    for item in results[:10]:
        if isinstance(item, dict):
            display = item.get("suggestion") or item.get("label") or item.get("name") or str(item)
        else:
            display = str(item)
        console.print(f"  [cyan]•[/cyan] {display}")


# ── Open in browser ────────────────────────────────────────────────────────────

@app.command()
def browse(
    suburb: str = typer.Argument(..., help="Suburb name"),
    state: str = typer.Argument(..., help="State abbreviation"),
    listing_type: str = typer.Option("sale", "--type", "-t", help="sale or rent"),
):
    """Open domain.com.au search in your browser."""
    import webbrowser
    slug = suburb.lower().replace(" ", "-") + "-" + state.lower()
    url = f"https://www.domain.com.au/{listing_type}/{slug}/"
    webbrowser.open(url)
    console.print(f"[green]Opened:[/green] {url}")


if __name__ == "__main__":
    app()
