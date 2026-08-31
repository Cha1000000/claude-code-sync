# Сценарий демонстрации для vhs: одна сессия переезжает с машины на машину.
# Стенд поднимает demo/prepare.sh; всё, что видно в кадре, — настоящий вывод.
#
#   bash demo/prepare.sh && vhs demo/demo.tape

Output docs/demo.gif

Require bash

Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 520
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "bash --norc --noprofile" Enter
Sleep 500ms
Type "source /tmp/ccsync-demo/env.sh && clear" Enter
Sleep 500ms
Show

Type "# a session I have been working on, here on the desktop" Enter
Sleep 1200ms
Type "ccsync push all" Enter
Sleep 3500ms

Hide
Type "use_machine laptop && clear" Enter
Sleep 300ms
Show

Type "# same vault on the laptop: another machine, unrelated paths" Enter
Sleep 1500ms
Type "ccsync pull all" Enter
Sleep 4000ms
Type "ls ~/.claude/projects/*myapp*/" Enter
Sleep 2500ms
Type "# right where Claude Code looks for it — /resume picks the conversation up" Enter
Sleep 1800ms
Type "head -c 113 ~/.claude/projects/*myapp*/*.jsonl; echo" Enter
Sleep 2800ms
Type "# note the cwd: paths inside the transcript were rewritten for this machine" Enter
Sleep 2000ms
Type "python3 ~/claude-code-sync/bin/session-context.py" Enter
Sleep 4000ms
