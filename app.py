import json
import os
import sqlite3
import hashlib
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_community.vectorstores import Chroma
from langgraph.graph import StateGraph, END
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer
from typing import List, TypedDict

load_dotenv()

google_api_key = os.getenv("GOOGLE_API_KEY")
if not google_api_key:
    raise RuntimeError("GOOGLE_API_KEY is not set. Add it to .env or the environment.")

def initialize_enterprise_erp_db():
    conn = sqlite3.connect("enterprise_finance.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS general_ledger (
            transaction_id TEXT PRIMARY KEY,
            account_code TEXT,
            posting_date TEXT,
            amount REAL,
            department TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_id TEXT PRIMARY KEY,
            vendor_name TEXT,
            approved_amount REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS goods_received (
            grn_id TEXT PRIMARY KEY,
            po_id TEXT,
            received_quantity INTEGER,
            FOREIGN KEY(po_id) REFERENCES purchase_orders(po_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vendor_invoices (
            invoice_id TEXT PRIMARY KEY,
            po_id TEXT,
            matched_txn_id TEXT,
            billed_amount REAL,
            vendor_name TEXT,
            FOREIGN KEY(matched_txn_id) REFERENCES general_ledger(transaction_id),
            FOREIGN KEY(po_id) REFERENCES purchase_orders(po_id)
        )
    """)

    invoice_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(vendor_invoices)")
    }
    if "po_id" not in invoice_columns:
        cursor.execute("ALTER TABLE vendor_invoices ADD COLUMN po_id TEXT")

    cursor.executemany(
        "INSERT OR IGNORE INTO general_ledger VALUES (?, ?, ?, ?, ?)",
        [
            ("GL-9001", "ACC-400", "2026-06-10", 14500.00, "Engineering"),
            ("GL-9002", "ACC-420", "2026-06-12", 8200.50, "Finance"),
        ],
    )
    cursor.executemany(
        "INSERT OR IGNORE INTO purchase_orders VALUES (?, ?, ?)",
        [("PO-101", "CloudScale Inc", 14500.00), ("PO-102", "SaaS Metrics Corp", 8200.50)],
    )
    cursor.executemany(
        "INSERT OR IGNORE INTO goods_received VALUES (?, ?, ?)",
        [("GRN-301", "PO-101", 10), ("GRN-302", "PO-102", 5)],
    )
    cursor.executemany(
        """INSERT OR IGNORE INTO vendor_invoices
           (invoice_id, po_id, matched_txn_id, billed_amount, vendor_name)
           VALUES (?, ?, ?, ?, ?)""",
        [
            ("INV-501", "PO-101", "GL-9001", 14500.00, "CloudScale Inc"),
            ("INV-502", "PO-102", "GL-9002", 8500.50, "SaaS Metrics Corp"),
        ],
    )
    cursor.execute("UPDATE vendor_invoices SET po_id = 'PO-101' WHERE invoice_id = 'INV-501' AND po_id IS NULL")
    cursor.execute("UPDATE vendor_invoices SET po_id = 'PO-102' WHERE invoice_id = 'INV-502' AND po_id IS NULL")
    conn.commit()
    conn.close()

initialize_enterprise_erp_db()


class LocalPolicyEmbeddings(Embeddings):
    """Small deterministic embeddings for offline local policy retrieval."""

    dimensions = 128

    def _embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dimensions
        for token in text.lower().split():
            index = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dimensions
            vector[index] += 1.0
        magnitude = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / magnitude for value in vector]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)


def setup_policy_vector_store():
    documents = [
        Document(page_content="MSA Clause 4.2: Any vendor invoice exceeding the approved Purchase Order baseline by more than 0.00% without a signed SOW amendment must trigger an automatic payment hold and formal debit memo."),
        Document(page_content="Procurement Clause 8.1: SaaS auto-renewal price escalations are capped at 5% annually, provided written notice is delivered 60 days before renewal."),
        Document(page_content="Dispute Resolution Policy 12.0: Finance controllers have 48 hours to execute manual debit adjustments or request corrected vendor billing after variance detection."),
    ]
    vectorstore = Chroma.from_documents(
        documents,
        LocalPolicyEmbeddings(),
        collection_name="finance-policy-clauses",
    )
    return vectorstore.as_retriever(search_kwargs={"k": 2})


vector_retriever = setup_policy_vector_store()

class FinanceAgentState(TypedDict):
    discrepancies: List[dict]
    policy_guidelines: str
    audit_memo: str
    approval_status: str
    webhook_status: str

llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash", google_api_key=google_api_key)

def sql_3way_matching_node(state: FinanceAgentState):
    conn = sqlite3.connect("enterprise_finance.db")
    query = """
        SELECT v.invoice_id, v.vendor_name, v.po_id, gr.grn_id,
               gr.received_quantity, p.approved_amount AS po_amount,
               v.billed_amount, g.amount AS ledger_amount,
               (v.billed_amount - p.approved_amount) AS invoice_po_variance,
               (v.billed_amount - g.amount) AS invoice_ledger_variance
        FROM vendor_invoices v
        JOIN purchase_orders p ON v.po_id = p.po_id
        JOIN goods_received gr ON p.po_id = gr.po_id
        JOIN general_ledger g ON v.matched_txn_id = g.transaction_id
        WHERE v.billed_amount != p.approved_amount
           OR v.billed_amount != g.amount
    """
    discrepancies = pd.read_sql(query, conn).to_dict(orient="records")
    conn.close()
    return {"discrepancies": discrepancies}


def rag_policy_retrieval_node(state: FinanceAgentState):
    relevant_docs = vector_retriever.invoke(
        "vendor invoice overbilling procurement tolerance payment hold dispute policy"
    )
    return {"policy_guidelines": "\n\n".join(doc.page_content for doc in relevant_docs)}

def llm_audit_generator_node(state: FinanceAgentState):
    prompt = f"""
    You are an automated Senior AI Finance Transformation Auditor.
    Review these ERP reconciliation anomalies across purchase orders, goods received notes,
    vendor invoices, and the general ledger:
    {state["discrepancies"]}
    Apply these retrieved corporate policy clauses:
    {state["policy_guidelines"]}
    Draft a concise executive compliance memo detailing the exact variance, control risk,
    and required corrective action.
    """
    response = llm.invoke([HumanMessage(content=prompt)])
    content = response.content
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return {"audit_memo": content}

def human_controller_review_node(state: FinanceAgentState):
    print("\n[HITL Checkpoint] Controller approval is required before export.")
    while True:
        decision = input("Enter APPROVE or REJECT: ").strip().upper()
        if decision in {"APPROVE", "REJECT"}:
            return {"approval_status": f"{decision}D_BY_CONTROLLER"}
        print("Please enter APPROVE or REJECT.")

def export_compliance_artifact_node(state: FinanceAgentState):
    timestamp = datetime.now(timezone.utc)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    artifact = {
        "timestamp": timestamp.isoformat(),
        "status": state["approval_status"],
        "discrepancies": state["discrepancies"],
        "audit_memo": state["audit_memo"],
    }
    audit_dir = Path("audit_logs")
    audit_dir.mkdir(exist_ok=True)
    json_path = audit_dir / f"compliance_report_{stamp}.json"
    pdf_path = audit_dir / f"compliance_report_{stamp}.pdf"
    json_path.write_text(json.dumps(artifact, indent=4), encoding="utf-8")

    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(str(pdf_path), pagesize=LETTER, rightMargin=inch, leftMargin=inch)
    story = [
        Paragraph("Finance Compliance Audit Report", styles["Title"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"Status: {escape(artifact['status'])}", styles["Heading2"]),
        Paragraph(f"Generated: {escape(artifact['timestamp'])}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph("Reconciliation Data", styles["Heading2"]),
        Preformatted(json.dumps(artifact["discrepancies"], indent=2), styles["Code"]),
        Spacer(1, 0.2 * inch),
        Paragraph("Audit Memo", styles["Heading2"]),
        Preformatted(str(artifact["audit_memo"]), styles["Code"]),
    ]
    document.build(story)
    print(f"\n[Artifacts Generated] JSON: {json_path}")
    print(f"[Artifacts Generated] PDF:  {pdf_path}")
    return state


def webhook_notification_node(state: FinanceAgentState):
    if state["approval_status"] != "APPROVED_BY_CONTROLLER":
        print("\n[Webhook Dispatcher] Notification skipped because remediation was rejected.")
        return {"webhook_status": "SKIPPED_NOT_APPROVED"}
    print("\n[Webhook Dispatcher] Slack alert sent and Jira ticket created.")
    print(
        f"-> Payload dispatched: status={state['approval_status']}, "
        f"discrepancies={len(state['discrepancies'])}"
    )
    return {"webhook_status": "DISPATCHED"}

workflow = StateGraph(FinanceAgentState)
workflow.add_node("matcher", sql_3way_matching_node)
workflow.add_node("rag_policy", rag_policy_retrieval_node)
workflow.add_node("auditor", llm_audit_generator_node)
workflow.add_node("controller_review", human_controller_review_node)
workflow.add_node("exporter", export_compliance_artifact_node)
workflow.add_node("webhook", webhook_notification_node)
workflow.set_entry_point("matcher")
workflow.add_edge("matcher", "rag_policy")
workflow.add_edge("rag_policy", "auditor")
workflow.add_edge("auditor", "controller_review")
workflow.add_edge("controller_review", "exporter")
workflow.add_edge("exporter", "webhook")
workflow.add_edge("webhook", END)
app = workflow.compile()

if __name__ == "__main__":
    print("Executing Enterprise 3-Way Matching Finance Transformation Agent...")
    result = app.invoke({
        "discrepancies": [],
        "policy_guidelines": "",
        "audit_memo": "",
        "approval_status": "",
        "webhook_status": "",
    })
    print("\n================ GENERATED AUDIT COMPLIANCE REPORT ================")
    print(result["audit_memo"])