# Tool Accuracy Statistics

Per-tool accuracy analysis, visualizations, and CSV generation for both Omen and Polymarket prediction tools.

## Scripts

| Script | Description |
|--------|-------------|
| `tool_accuracy.py` | Per-tool accuracy stats from predict-omen subgraph |
| `tool_accuracy_bars.py` | Grouped bar chart of accuracy by time bin |
| `tool_accuracy_by_side.py` | Accuracy broken down by Yes/No prediction side |
| `tool_accuracy_timeline.py` | Per-tool accuracy trend lines over time |
| `tool_accuracy_polymarket.py` | Tool accuracy stats using Polymarket subgraphs |
| `generate_accuracy_csv.py` | Generate accuracy CSV from on-chain Omen data |
| `superforcaster_trend.py` | Superforcaster cumulative/rolling accuracy trend on Polymarket |
| `accuracy_significance.py` | Statistical significance tests for all tool accuracy trends |
| `full_tool_analysis.py` | Comprehensive per-tool analysis using cached on-chain Polymarket data |
| `full_tool_analysis_report.py` | Cross-platform report generator using production accuracy endpoint |

## Usage

```bash
# Omen tool accuracy
poetry run python tool-accuracy/tool_accuracy.py

# Polymarket tool accuracy (last 100 bets)
poetry run python tool-accuracy/tool_accuracy_polymarket.py 100

# Generate CSV
poetry run python tool-accuracy/generate_accuracy_csv.py

# Superforcaster trend analysis
poetry run python tool-accuracy/superforcaster_trend.py              # all time
poetry run python tool-accuracy/superforcaster_trend.py --window 20  # smaller rolling window
poetry run python tool-accuracy/superforcaster_trend.py --days 30    # last 30 days

# Statistical significance tests
poetry run python tool-accuracy/accuracy_significance.py

# Full tool analysis (on-chain data)
poetry run python tool-accuracy/full_tool_analysis.py

# Generate cross-platform report (production endpoint)
poetry run python tool-accuracy/full_tool_analysis_report.py
```
