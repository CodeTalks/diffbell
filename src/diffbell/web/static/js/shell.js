// Shell words for the commands the page shows and copies (Plan 7 spec §2.5). Pure.
// The page never runs a command: it builds the text, which the person copies.
// shellQuote is kept equal to Python's shlex.quote (tests/web/test_js_actions.py
// compares them on awkward names).

// shlex.quote's safe characters: ASCII letters and digits and %+,-./:=@_ . A word
// made only of them is returned as it is; anything else is single-quoted.
const SAFE = /^[A-Za-z0-9_%+,\-./:=@]+$/;

export function shellQuote(word) {
  const s = String(word);
  if (s === "") return "''";
  if (SAFE.test(s)) return s;
  return `'${s.replaceAll("'", `'"'"'`)}'`;
}

// The history of one file, renames followed. --literal-pathspecs: a name with *, ?
// or [ means exactly that file (as in diffing._git).
export function gitLogCommand(folder, path) {
  return `git -C ${shellQuote(folder)} --literal-pathspecs log -p --follow -- ${shellQuote(path)}`;
}
