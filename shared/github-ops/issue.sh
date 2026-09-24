#!/usr/bin/env bash
# issue.sh -- one GitHub issue verb per call, with the PAT git already holds.
#
#   issue.sh create <file> [label,label]   file = "<title>\n<body markdown>"; prints "<number> <url>"
#   issue.sh get <n>                       title, labels, body
#   issue.sh comments <n>                  every comment (created_at, author, body)
#   issue.sh comment <n> <file>            post the file as a comment
#   issue.sh body <n> <file>               replace the body with the file (PATCH)
#   issue.sh setline <n> "<marker>" "<v>"  rewrite the value after a bold marker such as
#                                          "**修复 commit**" on the body line that carries it
#                                          (appends "- <marker> <v>" when absent)
#   issue.sh close <n> [completed|not_planned]
#   issue.sh list [open|closed|all]        "<number>\t<state>\t<title>" per line
#
# No `gh`. `git` itself cannot create issues (they are not in the repository), but the
# credential git pushes with -- `git credential fill` for host github.com -- is a PAT
# that can call the REST API, provided it has the Issues scope. github-remote.sh does
# the two things that matter: it never puts the token on a command line (curl reads
# the header from --config on a process substitution), and it never prints it.
# Run from inside the repository whose remote is the target; --repo overrides.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_OVERRIDE=""
if [[ "${1:-}" == "--repo" ]]; then REPO_OVERRIDE="$2"; shift 2; fi
op="${1:-}"; shift || true
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"
if [[ -n "$REPO_OVERRIDE" ]]; then GITHUB_OWNER="${REPO_OVERRIDE%%/*}"; GITHUB_REPO="${REPO_OVERRIDE##*/}"; fi
_init_github_auth || { echo "issue.sh: no GitHub token in the git credential helper (git credential fill, host github.com)" >&2; exit 2; }
_source_github_auth
API="https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO"
T="$(mktemp -d)"; trap 'rm -rf "$T"; _cleanup_github_auth' EXIT

json_field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],""))' "$1"; }

case "$op" in
  create)
    f="${1:?file}"; labels="${2:-}"
    python3 - "$f" "$labels" > "$T/create.json" <<'PY'
import json,sys
t=open(sys.argv[1]).read().split("\n",1)
print(json.dumps({"title":t[0].strip(),"body":t[1] if len(t)>1 else "",
                  "labels":[l for l in sys.argv[2].split(",") if l]}))
PY
    _auth_curl -X POST "$API/issues" --data-binary @"$T/create.json" > "$T/resp.json"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("number"), d.get("html_url")) if "number" in d else (sys.stderr.write(json.dumps(d)[:400]+"\n"), sys.exit(1))' "$T/resp.json";;
  get)
    _auth_curl "$API/issues/${1:?n}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["title"]); print([l["name"] for l in d["labels"]], d["state"]); print(d["body"] or "")';;
  comments)
    _auth_curl "$API/issues/${1:?n}/comments?per_page=100" | python3 -c 'import json,sys
for c in json.load(sys.stdin): print("-----", c["created_at"], c["user"]["login"]); print(c["body"])';;
  comment)
    python3 -c 'import json,sys; print(json.dumps({"body":open(sys.argv[1]).read()}))' "${2:?file}" > "$T/c.json"
    _auth_curl -o /dev/null -w 'POST %{http_code}\n' -X POST "$API/issues/${1:?n}/comments" --data-binary @"$T/c.json";;
  body)
    python3 -c 'import json,sys; print(json.dumps({"body":open(sys.argv[1]).read()}))' "${2:?file}" > "$T/b.json"
    _auth_curl -o /dev/null -w 'PATCH %{http_code}\n' -X PATCH "$API/issues/${1:?n}" --data-binary @"$T/b.json";;
  setline)
    n="${1:?n}"; marker="${2:?marker}"; val="${3:?value}"
    _auth_curl "$API/issues/$n" > "$T/i.json"
    python3 - "$T/i.json" "$marker" "$val" > "$T/p.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); marker=sys.argv[2]; val=sys.argv[3]
lines=(d["body"] or "").splitlines(); done=False
for i,l in enumerate(lines):
    if marker in l:
        pre=l.split(marker)[0]; lines[i]=pre+marker+" "+val; done=True; break
if not done: lines.append("- "+marker+" "+val)
print(json.dumps({"body":"\n".join(lines)}))
sys.stderr.write("line: "+[l for l in lines if marker in l][0]+"\n")
PY
    _auth_curl -o /dev/null -w 'PATCH %{http_code}\n' -X PATCH "$API/issues/$n" --data-binary @"$T/p.json";;
  close)
    reason="${2:-completed}"
    _auth_curl -o /dev/null -w 'PATCH %{http_code}\n' -X PATCH "$API/issues/${1:?n}" --data-binary "{\"state\":\"closed\",\"state_reason\":\"$reason\"}";;
  list)
    _auth_curl "$API/issues?state=${1:-open}&per_page=100" > "$T/list.json"
    python3 - "$T/list.json" <<'PY'
import json,sys
for i in json.load(open(sys.argv[1])):
    if "pull_request" in i: continue
    print("%d\t%s\t%s" % (i["number"], i["state"], i["title"]))
PY
    ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 2;;
esac
