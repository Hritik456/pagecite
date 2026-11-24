"""P4 retrieval: decomposition -> FT.HYBRID (text + vector, RRF, tenant pre-filter) -> rerank -> cited answer.

Entry points: pipeline.retrieve() and pipeline.answer_events(); routes/ask.py serves them as SSE.
"""
