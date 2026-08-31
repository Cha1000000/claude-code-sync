# Короткая запись для README: что видит человек на второй машине.
# Длинного объяснения здесь нет намеренно — его берёт на себя схема
# docs/how-it-works.svg, а запись лишь показывает, что это правда.
#
#   bash demo/prepare.sh && vhs demo/demo.tape

Output docs/demo.gif

Require bash

Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 330
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "bash --norc --noprofile" Enter
Sleep 400ms
Type "source /tmp/ccsync-demo/env.sh" Enter
Type "ccsync push all >/dev/null 2>&1" Enter
Type "use_machine laptop && clear" Enter
Sleep 400ms
Show

Type "# a laptop that has never seen this project" Enter
Sleep 1400ms
Type "ccsync pull all" Enter
Sleep 4000ms
Type "ls ~/.claude/projects/*myapp*/" Enter
Sleep 2600ms
Type "# the desktop session is here — claude --resume opens it" Enter
Sleep 2600ms
