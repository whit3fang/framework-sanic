import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import NotesPanel from "../src/components/NotesPanel.vue";
import RematchDialog from "../src/components/RematchDialog.vue";

const manifest = { connector: "fake", display_name: "Fake", object_label: "Contact", fields: [], search_placeholder: "Search" };

describe("NotesPanel", () => {
  it("reuses the client-generated note id when retrying", async () => {
    const addNote = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({});
    const w = mount(NotesPanel, { props: { api: { addNote } as never, callId: "c1" } });
    await w.find("[data-test=note-body]").setValue("follow up");
    await w.find("form").trigger("submit");
    await flushPromises();
    await w.find("form").trigger("submit");
    await flushPromises();
    expect(addNote).toHaveBeenCalledTimes(2);
    expect(addNote.mock.calls[0]![0]).toBe(addNote.mock.calls[1]![0]);
    expect(w.find("[data-test=note-saved]").exists()).toBe(true);
  });
});

describe("RematchDialog", () => {
  it("requires the forward-only confirmation before rematching", async () => {
    const entity = { ref: { object_type: "contact", id: "7" }, properties: { name: "Bob" } };
    const api = { searchContacts: vi.fn().mockResolvedValue({ results: [entity] }), rematch: vi.fn().mockResolvedValue({}) };
    const w = mount(RematchDialog, { props: { api: api as never, dialpadRef: "phone:+1", manifest } });
    await w.find("[data-test=search]").setValue("Bob");
    await w.find("form").trigger("submit");
    await flushPromises();
    expect(w.find("[data-test=forward-only]").exists()).toBe(false);
    await w.find("input[type=radio]").trigger("change");
    expect(w.find("[data-test=forward-only]").text()).toContain("future calls");
    await w.find("[data-test=confirm]").trigger("click");
    await flushPromises();
    expect(api.rematch).toHaveBeenCalledWith("phone:+1", entity.ref);
    expect(w.emitted("done")).toHaveLength(1);
  });
});
