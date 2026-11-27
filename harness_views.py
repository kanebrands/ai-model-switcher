"""Curated links to coding harnesses that can use a local API."""
from benchmark_views import esc, shell


# These links point to project-maintained documentation, not third-party installers.
HARNESSES = (
    ("Hermes Agent", "Persistent assistant across terminal and messaging channels",
     "Memory, skills, automation, and multiple execution backends.",
     "More setup and permission decisions than a focused coding tool; small local models may struggle with long tool loops.",
     "https://hermes-agent.nousresearch.com/docs/getting-started/quickstart",
     "https://hermes-agent.nousresearch.com/docs/user-guide/configuration"),
    ("OpenCode", "Terminal and IDE coding with model choice",
     "Agent workflows, file edits, and flexible custom providers in one interface.",
     "Custom models need explicit endpoint, limits, and capability settings; tool-call reliability depends on the model.",
     "https://opencode.ai/docs/", "https://opencode.ai/docs/providers/"),
    ("Pi", "Lean terminal coding agent and custom extensions",
     "Small core with read, write, edit, and shell tools; custom OpenAI-compatible models.",
     "Less guided setup; extensions can run code with the user's privileges, so review them before installation.",
     "https://github.com/earendil-works/pi/tree/main/packages/coding-agent",
     "https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md"),
    ("Aider", "Focused edits in an existing Git repository",
     "Git-aware changes, readable diffs, and a low-overhead terminal workflow.",
     "Less suited to broad autonomous workflows; some local models fail to produce valid edits.",
     "https://aider.chat/docs/install.html", "https://aider.chat/docs/llms/openai-compat.html"),
    ("Cline", "Agentic coding inside VS Code",
     "Visual file review, checkpoints, and explicit approval controls.",
     "Multi-step runs can be slow on local models; tool use and context capacity matter.",
     "https://docs.cline.bot/getting-started/installing-cline",
     "https://docs.cline.bot/provider-config/openai-compatible"),
    ("Goose", "Extensible terminal or desktop agent",
     "Extensions and MCP tools support tasks beyond editing code.",
     "More moving parts than a minimal editor; requires reliable tool calling from the selected model.",
     "https://github.com/aaif-goose/goose",
     "https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/providers.md"),
)


def _link(url, label):
    return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(label)}</a>'


def index():
    rows = "".join(
        f'<article class="card harness-card"><div><h2>{esc(name)}</h2><p class="harness-purpose">{esc(purpose)}</p></div>'
        f'<div><h3>Strengths</h3><p>{esc(strength)}</p><h3>Tradeoffs</h3><p>{esc(weakness)}</p></div>'
        f'<div class="harness-links">{_link(install, "Install guide")}{_link(connect, "Local API setup")}</div></article>'
        for name, purpose, strength, weakness, install, connect in HARNESSES
    )
    body = '''<style>.harness-intro{max-width:75rem}.harness-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}.harness-card{margin:0;display:flex;flex-direction:column;justify-content:space-between}.harness-card h2{margin:.1rem 0 .3rem}.harness-card h3{font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#e9bc82;margin:1rem 0 .2rem}.harness-card p{margin:.2rem 0}.harness-purpose{color:#b8d0ca}.harness-links{display:flex;flex-wrap:wrap;gap:.65rem;margin-top:1.2rem}.harness-links a{padding:.5rem .75rem;border:1px solid #52706c;border-radius:.55rem;text-decoration:none;background:#203438}@media(max-width:800px){.harness-grid{grid-template-columns:1fr}}</style>
<section class="card harness-intro"><h2>Connect a harness to this server</h2><ol><li>Install the harness on the computer where it should edit files. These links do not install software on the switcher host.</li><li>Use the API base <code>http://&lt;server-LAN-IP&gt;:8080/v1</code> and a dedicated key from <a href="/sharing">Client sharing</a>.</li><li>Read <code>GET /v1/models</code> with that key and enter the returned model ID in the harness. Switching models can change that ID.</li><li>Choose an OpenAI-compatible <strong>Chat Completions</strong> provider where offered. Test a read, a file edit, and a tool call before allowing autonomous work.</li></ol><p class="note">Compatibility and speed depend on the active engine, model, context, and tool-call format. A successful chat response does not prove that agent tools work. On an untrusted network, put the API behind HTTPS before sending keys.</p></section>
<h2>Choose a harness</h2><p class="note">The tradeoffs below are practical guidance based on each project's documented features; they are not benchmark results for the currently selected model.</p><div class="harness-grid">''' + rows + '</div>'
    return shell("Coding harnesses", body)
