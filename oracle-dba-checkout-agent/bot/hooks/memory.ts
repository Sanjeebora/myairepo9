// Journals every turn (timestamp, session, user message, result) so
// future sessions can recall past work. The journal is shared across every
// session — and every user — of this agent. Pass your own MemoryBackend to
// change where memories live, or delete this file to opt out.
import { memoryHook } from "@cursor/bdk/memory";

export default memoryHook();
