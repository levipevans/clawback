from __future__ import annotations

from clawback.memory import Conversation, MemoryStore


def test_conversation_keeps_pairs_and_trims():
    conversation = Conversation(max_messages=4, max_chars=10_000)
    for index in range(5):
        conversation.add_user(f"u{index}")
        conversation.add_assistant(f"a{index}")
    messages = conversation.messages()
    assert len(messages) == 4
    assert messages[0] == {"role": "user", "content": "u3"}
    assert messages[-1] == {"role": "assistant", "content": "a4"}


def test_conversation_char_budget():
    conversation = Conversation(max_messages=100, max_chars=50)
    conversation.add_user("x" * 40)
    conversation.add_assistant("y" * 40)
    conversation.add_user("z" * 40)
    conversation.add_assistant("w" * 40)
    assert conversation._size() <= 50
    assert all(message["role"] == "user" for message in conversation.messages()) or not conversation.messages()


def test_conversation_reset():
    conversation = Conversation()
    conversation.add_user("hello")
    conversation.add_assistant("hi")
    conversation.reset()
    assert conversation.messages() == []


def test_memory_append_and_context(tmp_path):
    memory = MemoryStore(tmp_path / "memory.md")
    memory.append_interaction("please summarize /work", "It has two files.", ["list_dir", "read_file"])
    text = memory.read()
    assert "# Clawback memory" in text
    assert "please summarize /work" in text
    assert "It has two files." in text
    assert "list_dir, read_file" in text
    assert "please summarize" in memory.context()


def test_memory_note(tmp_path):
    memory = MemoryStore(tmp_path / "memory.md")
    memory.append_note("remember the project is called Clawback")
    assert "remember the project is called Clawback" in memory.read()


def test_memory_is_trimmed_but_keeps_recent(tmp_path):
    memory = MemoryStore(tmp_path / "memory.md", max_chars=900)
    for index in range(40):
        memory.append_interaction(f"user {index}", f"assistant {index}", [])
    text = memory.read()
    assert len(text) <= 1100
    assert "older memory trimmed" in text
    assert "user 39" in text
    assert "user 0" not in text


def test_memory_context_is_tail(tmp_path):
    memory = MemoryStore(tmp_path / "memory.md")
    memory.append_interaction("early", "old answer", [])
    memory.append_interaction("late", "new answer", [])
    context = memory.context(max_chars=120)
    assert "late" in context
    assert "new answer" in context
    assert context.startswith("...")


def test_memory_clear(tmp_path):
    memory = MemoryStore(tmp_path / "memory.md")
    memory.append_note("something")
    assert memory.read()
    memory.clear()
    assert memory.read() == ""
    assert memory.size() == 0
