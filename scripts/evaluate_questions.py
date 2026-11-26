"""
Evaluasi chatbot (Qwen asli + MCP + PostgreSQL) terhadap bank pertanyaan.

Untuk setiap pertanyaan dicatat: tool yang dipilih Qwen, apakah tool itu
termasuk acceptable_tools, apakah semua tool berhasil, durasi, dan jawaban.
Hasil disimpan ke reports/evaluasi_<waktu>.md dan .csv.

Contoh:
    python scripts/evaluate_questions.py
    python scripts/evaluate_questions.py --ids 1 5 25-30
    python scripts/evaluate_questions.py --category biaya
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ai.qwen_mcp_client import BPJSChatbot  # noqa: E402

DEFAULT_BANK = PROJECT_ROOT / "tests" / "questions" / "question_bank.yaml"
REPORT_DIR = PROJECT_ROOT / "reports"


def parse_ids(values):
    ids = set()
    for value in values or []:
        if "-" in value:
            start, end = value.split("-", 1)
            ids.update(range(int(start), int(end) + 1))
        else:
            ids.add(int(value))
    return ids


def load_questions(path, ids=None, category=None):
    questions = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["questions"]
    if ids:
        questions = [item for item in questions if item["id"] in ids]
    if category:
        questions = [item for item in questions if item.get("category") == category]
    return questions


async def evaluate(questions, timeout):
    results = []

    async with BPJSChatbot() as chatbot:
        for item in questions:
            # Setiap pertanyaan diuji pada percakapan baru agar independen.
            conversation_id = f"eval-{item['id']}"
            chatbot.reset_conversation(conversation_id)

            started = time.perf_counter()
            error = None
            answer = ""

            try:
                answer = await asyncio.wait_for(
                    chatbot.ask(item["question"], conversation_id=conversation_id),
                    timeout=timeout,
                )
            except Exception as exc:  # dicatat di laporan
                error = f"{type(exc).__name__}: {exc}"

            duration = time.perf_counter() - started
            trace = list(chatbot.last_trace)
            tools = [entry["tool"] for entry in trace]
            acceptable = set(item.get("acceptable_tools") or [])

            result = {
                "id": item["id"],
                "category": item.get("category", ""),
                "question": item["question"],
                "tools_used": tools,
                "tool_match": bool(tools) and any(tool in acceptable for tool in tools),
                "tools_success": bool(trace) and all(entry["success"] for entry in trace),
                "answered": error is None and bool(answer.strip()),
                "duration_seconds": round(duration, 1),
                "error": error,
                "answer": answer,
                "trace": trace,
            }
            result["passed"] = (
                result["tool_match"] and result["tools_success"] and result["answered"]
            )
            results.append(result)

            status = "LULUS" if result["passed"] else "PERIKSA"
            print(
                f"[{status}] Q{item['id']:>3} {duration:5.1f}s "
                f"tools={tools or '-'}  {item['question'][:60]}"
            )

            chatbot.conversations.pop(conversation_id, None)

    return results


def write_reports(results):
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORT_DIR / f"evaluasi_{stamp}.csv"
    md_path = REPORT_DIR / f"evaluasi_{stamp}.md"

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "id", "category", "question", "passed", "tool_match",
            "tools_success", "answered", "tools_used", "duration_seconds",
            "error", "answer",
        ])
        for row in results:
            writer.writerow([
                row["id"], row["category"], row["question"], row["passed"],
                row["tool_match"], row["tools_success"], row["answered"],
                ", ".join(row["tools_used"]), row["duration_seconds"],
                row["error"] or "", row["answer"],
            ])

    total = len(results)
    passed = sum(row["passed"] for row in results)
    lines = [
        "# Laporan Evaluasi Chatbot",
        "",
        f"Waktu: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"**Lulus: {passed}/{total}** "
        f"({(passed / total * 100) if total else 0:.1f}%)",
        "",
        "Lulus = Qwen memilih tool yang tepat, semua tool berhasil, "
        "dan jawaban tidak kosong. Kebenaran narasi tetap perlu dicek manual.",
        "",
        "| ID | Kategori | Lulus | Tool dipakai | Durasi | Pertanyaan |",
        "|---|---|---|---|---|---|",
    ]
    for row in results:
        lines.append(
            f"| {row['id']} | {row['category']} | "
            f"{'✅' if row['passed'] else '⚠️'} | "
            f"{', '.join(row['tools_used']) or '-'} | "
            f"{row['duration_seconds']}s | {row['question']} |"
        )

    lines.append("")
    lines.append("## Detail jawaban")
    for row in results:
        lines.extend([
            "",
            f"### Q{row['id']}. {row['question']}",
            "",
            f"- Tool: {', '.join(row['tools_used']) or '-'}",
            f"- Argumen: `{json.dumps([entry['arguments'] for entry in row['trace']], ensure_ascii=False, default=str)}`",
        ])
        if row["error"]:
            lines.append(f"- Error: {row['error']}")
        failed = [entry for entry in row["trace"] if not entry["success"]]
        for entry in failed:
            lines.append(f"- Tool gagal `{entry['tool']}`: {entry['message']}")
        lines.extend(["", "```", row["answer"].strip() or "(kosong)", "```"])

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, csv_path, passed, total


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bank", default=str(DEFAULT_BANK))
    parser.add_argument("--ids", nargs="*", help="contoh: 1 5 10-20")
    parser.add_argument("--category")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    questions = load_questions(args.bank, parse_ids(args.ids), args.category)
    if not questions:
        print("Tidak ada pertanyaan yang cocok.")
        return 1

    print(f"Menguji {len(questions)} pertanyaan...\n")
    results = asyncio.run(evaluate(questions, args.timeout))
    md_path, csv_path, passed, total = write_reports(results)

    print(f"\nLulus {passed}/{total}")
    print(f"Laporan: {md_path}")
    print(f"CSV    : {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
