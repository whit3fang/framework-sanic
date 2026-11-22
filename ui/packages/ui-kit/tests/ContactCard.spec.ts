import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ContactCard from "../src/ContactCard.vue";

const manifest = {
  connector: "fake",
  display_name: "Fake CRM",
  object_label: "Contact",
  fields: [
    { key: "name", label: "Name", kind: "text" as const },
    { key: "email", label: "Email", kind: "email" as const },
  ],
  link_out_template: "https://crm.example/contacts/{id}",
  search_placeholder: "Search",
  create_fields: [{ key: "name", label: "Name", kind: "text" as const }],
};

describe("ContactCard", () => {
  it("renders manifest fields and the link-out", () => {
    const w = mount(ContactCard, {
      props: { manifest, entity: { external_id: "7", external_type: "contact", properties: { name: "Ada", email: "ada@x.io" } } },
    });
    expect(w.text()).toContain("Name");
    expect(w.text()).toContain("Ada");
    expect(w.find("[data-test=link-out]").attributes("href")).toBe("https://crm.example/contacts/7");
    expect(w.find("a[href^='mailto:']").exists()).toBe(true);
  });

  it("escapes external data and drops unsafe links", () => {
    const evil = '<img src=x onerror="alert(1)">';
    const w = mount(ContactCard, {
      props: { manifest, entity: { external_id: "1", external_type: "contact", properties: { name: evil }, url: "javascript:alert(1)" } },
    });
    expect(w.html()).not.toContain("<img");
    expect(w.text()).toContain(evil);
    expect(w.find("[data-test=link-out]").exists()).toBe(false);
  });

  it("offers create when there is no match", async () => {
    const w = mount(ContactCard, { props: { manifest, entity: null } });
    expect(w.find("[data-test=no-match]").exists()).toBe(true);
    await w.find("[data-test=create]").trigger("click");
    expect(w.emitted("create")).toHaveLength(1);
  });
});
