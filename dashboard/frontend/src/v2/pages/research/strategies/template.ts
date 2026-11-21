// The 1-based lines of a strategy's code that carry the ENTRY and EXIT markers (a comment line whose
// trimmed text starts with "# ENTRY" or "# EXIT"). Null when either marker is missing, so the editor
// then opens at line 1.
export function markerLines(code: string): { entry: number; exit: number } | null {
  let entry = 0;
  let exit = 0;
  code.split("\n").forEach((line, i) => {
    const t = line.trim();
    if (entry === 0 && t.startsWith("# ENTRY")) entry = i + 1;
    if (exit === 0 && t.startsWith("# EXIT")) exit = i + 1;
  });
  return entry > 0 && exit > 0 ? { entry, exit } : null;
}

// The blank strategy a new one starts from: the moving-average cross template with its default
// windows (fast 20, slow 50), the class docstring and the comments taken out, so ENTRY and EXIT
// stand alone for the person to fill in. `about` becomes the module docstring, which the server
// reads as the strategy's description; a backslash and a double quote are escaped and line breaks
// become spaces so the docstring stays one line.
export function blankTemplate(about: string): string {
  const doc = about.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r?\n/g, " ");
  return [
    `"""${doc}"""`,
    "from __future__ import annotations",
    "",
    "from strategies.base import RunResult, SettingRow, Strategy, StrategyInput",
    "from strategies.helpers import default_costs, long_only_signal_result",
    "",
    "",
    "class MyStrategy(Strategy):",
    '    asset_class_scope = ("crypto", "equities")',
    "",
    "    def run(self, data: StrategyInput, settings: list[SettingRow]) -> RunResult:",
    "        close = data.close",
    "        fast = close.rolling(20).mean()",
    "        slow = close.rolling(50).mean()",
    "",
    "        # ENTRY: when to buy",
    "        entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))",
    "",
    "        # EXIT: when to sell",
    "        exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))",
    "",
    "        fees, slippage = default_costs(settings)",
    "        return long_only_signal_result(",
    "            data, entries, exits, fees, slippage,",
    '            indicators={"fast_sma": fast, "slow_sma": slow},',
    "        )",
  ].join("\n");
}
