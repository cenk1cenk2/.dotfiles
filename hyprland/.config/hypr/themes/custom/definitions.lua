-- Based on Base16 Seti UI
-- Author: Appelgriebsch

local M = {}

M.gtk = {
  theme = "Graphite-yellow-Dark-compact",
  icon_theme = "Tela-yellow-dark",
  cursor_theme = "Breeze_Light",
  color_scheme = "prefer-dark",
}

M.font = {
  gui = "Segoe UI 12",
  term = "ConsolasLG Nerd Font 16",
}

-- Color palette (Base16 Seti UI). Indexed 0..16 to mirror $color0..$color16.
M.colors = {
  [0] = "rgb(282c34)",
  [1] = "rgb(e06c75)",
  [2] = "rgb(98c379)",
  [3] = "rgb(e5c07b)",
  [4] = "rgb(61afef)",
  [5] = "rgb(a40778)",
  [6] = "rgb(56b6c2)",
  [7] = "rgb(979eab)",
  [8] = "rgb(4b5263)",
  [9] = "rgb(ef9ea1)",
  [10] = "rgb(d4ff79)",
  [11] = "rgb(eed5a8)",
  [12] = "rgb(98caf6)",
  [13] = "rgb(ca6da4)",
  [14] = "rgb(94ced6)",
  [15] = "rgb(abb2bf)",
  [16] = "rgb(ececec)",
}

M.derived = {
  transparent_bg = "rgba(23, 25, 30, 0.4)",
  bg = "rgb(1e2127)",
  text = "rgb(eeeeee)",
  selection = "rgb(282a2b)",
  accent = "rgb(e5c07b)",
}

return M
