"""Phase 4 Stage 2: live execution progress over Server-Sent Events (in-memory, mock-only).

Import submodules directly (``app.streaming.emitter``, ``.broker``, ``.runner``). This
package deliberately imports nothing: the execution nodes import ``emitter``, and the
runner imports the execution service, so eager imports here would be circular.
"""
