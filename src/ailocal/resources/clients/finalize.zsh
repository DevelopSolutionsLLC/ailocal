# Swap p10k for a plain prompt in VS Code terminals; sourced last in ~/.zshrc.
# Managed by `ailocal clients`, overwritten; must print nothing at all.
# Shell integration keeps OSC 633 markers, or agent spinners never stop.
if [[ -n "$_AILOCAL_VSCODE" ]]; then
  (( $+functions[prompt_powerlevel9k_teardown] )) && prompt_powerlevel9k_teardown
  PROMPT='%~ %# '
  if command -v code >/dev/null 2>&1; then
    . "$(code --locate-shell-integration-path zsh)" 2>/dev/null
  fi
fi
