import os
import sqlite3
from langchain_groq import ChatGroq
from typing import Annotated, Dict, List, Optional, Any
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel
import warnings
warnings.filterwarnings("ignore")

load_dotenv()

console = Console()


class ParsedQuerySchema(BaseModel):
    intent: str = Field(description="Intent: 'query_db', 'general_chat', or 'unknown'")
    customer_name: Optional[str] = Field(default=None, description="Name of the customer if mentioned (first, last, or full)")
    product_name: Optional[str] = Field(default=None, description="Name of the product if mentioned")
    temporal_range: Optional[str] = Field(default=None, description="Time expressions like 'past_month', 'today', 'last_30_days', 'older'")
    target_table: Optional[str] = Field(default=None, description="Target table: 'orders', 'customers', 'products', 'interactions', or 'all'")


# Initialize Groq LLM after schema is defined
# GROQ_API_KEY should be stored in your .env file.
llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0)
structured_llm = llm.with_structured_output(ParsedQuerySchema)


class AgentState(Dict):
    messages: Annotated[list, add_messages]
    user_query: str
    is_ambiguous: bool
    clarifying_question: Optional[str]
    resolved_selection: Optional[str]
    sql_query: Optional[str]
    query_results: Optional[List[Dict[str, Any]]]
    error_message: Optional[str]
    parsed_criteria: Dict[str, Any]
    final_answer: Optional[str]


def _extract_text(response) -> str:
    """Helper to pull plain text out of a langchain response, handling list-of-parts content."""
    content = response.content
    if isinstance(content, list):
        content = next(
            (p.get('text', '') if isinstance(p, dict) else getattr(p, 'text', str(p)) for p in content),
            ''
        )
    return str(content).strip()


def query_analyzer(state: AgentState) -> Dict[str, Any]:
    """Analyzes user queries for intent, customer names and dates."""
    console.print("[dim]Reading your question...[/dim]")
    query = state.get('user_query')

    resolved = state.get('resolved_selection')
    criteria = state.get('parsed_criteria', {})

    if resolved:
        matching_details = criteria.get('matching_details', [])
        matching_ids = criteria.get('matching_ids', [])
        if resolved in matching_details:
            idx = matching_details.index(resolved)
            criteria['customer_id'] = matching_ids[idx]
            criteria['resolved_name'] = resolved

        console.print(f"[dim]Using your clarification -> customer_id {criteria.get('customer_id')}[/dim]")

        return {
            "is_ambiguous": False,
            "clarifying_question": None,
            "parsed_criteria": criteria
        }

    prompt = f"""You are an expert database analyzer. Extract search criteria from user's 
    natural language query.
    Current Date: {datetime.now().strftime("%Y-%m-%d")} (use this to determine temporal range)
    
    User Query : "{query}"
    """

    response = structured_llm.invoke(prompt)
    extracted = response.dict()
    console.print(f"[dim]Extracted: {extracted}[/dim]")

    parsed_criteria = {
        'customer_name': extracted.get('customer_name'),
        'product_name': extracted.get('product_name'),
        'temporal_range': extracted.get('temporal_range'),
        'target_table': extracted.get('target_table'),
        'intent': extracted.get('intent')
    }

    is_ambiguous = False
    clarifying_question = None
    matches = []

    name = extracted.get('customer_name')
    if name:
        conn = sqlite3.connect('customer_records.db')
        cursor = conn.cursor()

        cursor.execute(
            "SELECT customer_id, first_name, last_name, email FROM customers WHERE first_name LIKE ? OR last_name LIKE ?",
            (f"%{name}%", f"%{name}%")
        )

        matches = cursor.fetchall()
        conn.close()

    if len(matches) > 1:
        is_ambiguous = True
        options = [f"{m[1]} {m[2]} ({m[3]})" for m in matches]
        options_str = " OR ".join(f"[{opt}]" for opt in options)
        clarifying_question = f"I found multiple customers matching '{name}': {options_str}. Which one did you mean?"
        parsed_criteria['matching_ids'] = [m[0] for m in matches]
        parsed_criteria['matching_details'] = options
        console.print(f"[yellow]Ambiguous: {len(matches)} customers matched '{name}'[/yellow]")
    elif len(matches) == 1:
        parsed_criteria['customer_id'] = matches[0][0]
        parsed_criteria['resolved_name'] = f"{matches[0][1]} {matches[0][2]}"
        console.print(f"[dim]Matched customer_id {matches[0][0]} ({parsed_criteria['resolved_name']})[/dim]")
    else:
        console.print("[dim]No specific customer name matched — treating as a general query.[/dim]")

    return {
        'is_ambiguous': is_ambiguous,
        'clarifying_question': clarifying_question,
        'parsed_criteria': parsed_criteria
    }


def sql_generator(state: AgentState) -> Dict[str, Any]:
    """Generates SQLite-compliant SQL from parsed criteria."""
    criteria = state.get('parsed_criteria', {})
    user_query = state.get('user_query')
    today_str = datetime.now().strftime('%Y-%m-%d')
    past_month_str = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    prompt = f"""
    You are an expert SQL generator. Write a single SQLite SELECT statement to answer the user's query.
    Return ONLY the raw SQL code. No markdown formatting, no backticks, no markdown blocks, no comments.
    
    Database Schema:
    - customers (customer_id, first_name, last_name, email, city)
    - products (product_id, product_name, category, price)
    - orders (order_id, customer_id, product_id, quantity, amount, order_date, status)
    - interactions (interaction_id, customer_id, channel, summary, interaction_date)
    
    Current Date: {today_str}
    Past Month Boundary: {past_month_str}
    
    Extracted Criteria:
    {criteria}
    
    User Query: "{user_query}"
    
    SQL Rules:
    1. If customer_id is resolved in criteria ({criteria.get('customer_id')}), filter using: WHERE customer_id = {criteria.get('customer_id') or 'NULL'}
    2. If query mentions "past month" or "last month", filter dates to be between '{past_month_str}' and '{today_str}'.
    3. Make sure table joins are correct (e.g. JOIN products ON orders.product_id = products.product_id).
    """

    response = llm.invoke(prompt)
    sql = _extract_text(response).replace("```sql", "").replace("```", "").strip()

    # "thinking" trace: show the generated SQL, syntax highlighted
    console.print(Panel(Syntax(sql, "sql", theme="monokai", word_wrap=True), title="Generating query", border_style="cyan"))

    return {'sql_query': sql}


def query_executor(state: AgentState) -> Dict[str, Any]:
    """Executes the SQL query safely and saves the output."""
    sql = state.get('sql_query')
    if not sql:
        return {'error_message': 'No SQL query found to execute.'}

    try:
        conn = sqlite3.connect('customer_records.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(sql)
        rows = cursor.fetchall()
        results = [dict(row) for row in rows]
        conn.close()
        console.print(f"[dim]Query returned {len(results)} row(s)[/dim]")
        return {"query_results": results, 'error_message': None}
    except Exception as e:
        console.print(f"[red]Query failed: {str(e)}[/red]")
        return {"error_message": str(e)}


def response_synthesizer(state: AgentState) -> Dict[str, Any]:
    """Turns raw query results (or an error) into a clean natural-language answer.
    This is the ONLY thing meant to read as the 'final output' to the user —
    everything above is the thinking/trace."""
    results = state.get('query_results')
    error = state.get('error_message')
    user_query = state.get('user_query')

    if error:
        answer = f"I ran into a problem answering that: {error}"
    elif not results:
        answer = "I couldn't find any records matching that request."
    else:
        prompt = f"""The user asked: "{user_query}"

        Here are the raw query results (JSON):
        {results}

        Write a short, clear, natural-language answer to the user's question based on this data.
        Use a table or bullet list only if it genuinely helps readability.
        Do not mention SQL, databases, or the word "query" — just answer like a helpful assistant.
        """
        response = llm.invoke(prompt)
        answer = _extract_text(response)

    console.print(Panel(answer, title="Answer", border_style="green"))
    return {"final_answer": answer}


def clarification_handler(state: AgentState) -> Dict[str, Any]:
    """A node that acts as a placeholder when the graph interrupts for user feedback."""
    question = state.get("clarifying_question")
    if question:
        console.print(Panel(question, title="Need a bit more info", border_style="yellow"))
    return {}


def check_ambiguity_router(state: AgentState) -> str:
    """Routes to Clarification if ambiguity is detected, otherwise generates SQL."""
    if state.get("is_ambiguous"):
        return "clarify"
    return "generate_sql"


graph = StateGraph(AgentState)

graph.add_node('query_analyzer', query_analyzer)
graph.add_node('sql_generator', sql_generator)
graph.add_node('query_executor', query_executor)
graph.add_node('response_synthesizer', response_synthesizer)
graph.add_node('clarification_handler', clarification_handler)

graph.add_edge(START, 'query_analyzer')
graph.add_conditional_edges(
    'query_analyzer',
    check_ambiguity_router, {
        'clarify': "clarification_handler",
        'generate_sql': "sql_generator"
    }
)

graph.add_edge('sql_generator', 'query_executor')
graph.add_edge('query_executor', 'response_synthesizer')
graph.add_edge('response_synthesizer', END)
graph.add_edge("clarification_handler", 'query_analyzer')

memory = MemorySaver()

workflow = graph.compile(
    checkpointer=memory,
    interrupt_before=['clarification_handler']
)

console.print("[dim]LangGraph Agent structure compiled successfully![/dim]")
