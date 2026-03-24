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

## Usage

```bash
# Omen tool accuracy
poetry run python tool-accuracy/tool_accuracy.py

# Polymarket tool accuracy (last 100 bets)
poetry run python tool-accuracy/tool_accuracy_polymarket.py 100

# Generate CSV
poetry run python tool-accuracy/generate_accuracy_csv.py
```
