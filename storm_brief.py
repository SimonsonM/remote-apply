"""Stanford STORM company brief (runs in .venv-storm, which pins its own older DSPy).
Usage: .venv-storm/bin/python storm_brief.py "Company" out.md"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from knowledge_storm import STORMWikiLMConfigs, STORMWikiRunner, STORMWikiRunnerArguments
from knowledge_storm.lm import LitellmModel
from knowledge_storm.rm import DuckDuckGoSearchRM

company, out = sys.argv[1], Path(sys.argv[2])
kw = {"api_key": os.environ["ANTHROPIC_API_KEY"], "temperature": 1.0}
small = LitellmModel(model="anthropic/claude-haiku-4-5-20251001", max_tokens=600, **kw)
big = LitellmModel(model="anthropic/claude-haiku-4-5-20251001", max_tokens=3000, **kw)
cfg = STORMWikiLMConfigs()
cfg.set_conv_simulator_lm(small); cfg.set_question_asker_lm(small)
cfg.set_outline_gen_lm(big); cfg.set_article_gen_lm(big); cfg.set_article_polish_lm(big)
tmp = tempfile.mkdtemp()
args = STORMWikiRunnerArguments(output_dir=tmp, max_conv_turn=3, max_perspective=3, search_top_k=4)
runner = STORMWikiRunner(args, cfg, DuckDuckGoSearchRM(k=args.search_top_k, safe_search="On", region="us-en"))
topic = f"{company} (company): business, products, customers, recent news, layoffs and hiring, culture"
runner.run(topic=topic, do_research=True, do_generate_outline=True, do_generate_article=True, do_polish_article=True)
runner.post_run()
d = next(p for p in Path(tmp).iterdir() if p.is_dir())
art = (d / "storm_gen_article_polished.txt")
art = art if art.exists() else d / "storm_gen_article.txt"
urls = json.loads((d / "url_to_info.json").read_text()).get("url_to_unified_index", {}) if (d / "url_to_info.json").exists() else {}
src = "\n".join(f"[{i}] {u}" for u, i in sorted(urls.items(), key=lambda x: x[1]))
out.write_text(f"# {company} - STORM brief\n\n{art.read_text()}\n\n## Sources\n{src}\n")
