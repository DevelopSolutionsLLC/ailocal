# ailocal finalize.zsh — sourced at the BOTTOM of ~/.zshrc. Managed file —
# installed by scripts/install-clients.sh, always overwritten. Must produce
# ZERO stdout/stderr.
#
# Only acts inside a VS Code terminal (_AILOCAL_VSCODE set by configure.zsh,
# sourced at the top of .zshrc). Tears down the p10k prompt that instant-prompt
# still loaded, replaces it with a plain fast prompt, and explicitly sources
# VS Code's shell-integration script so OSC 633 command-completion markers are
# reliable — without this, agent terminal commands finish but the client's
# spinner never stops.
if [[ -n "$_AILOCAL_VSCODE" ]]; then
  (( $+functions[prompt_powerlevel9k_teardown] )) && prompt_powerlevel9k_teardown
  PROMPT='%~ %# '
  if command -v code >/dev/null 2>&1; then
    . "$(code --locate-shell-integration-path zsh)" 2>/dev/null
  fi
fi
