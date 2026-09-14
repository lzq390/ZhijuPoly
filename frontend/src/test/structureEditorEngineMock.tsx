// Business-page tests exercise the shared surface and navigation contract.
// The real SDK, DOM events and Worker lifecycle run in the browser gates.
export const engine = "react";
export default function StructureEditorEngineMock({ title }: { title: string }) {
  return <div title={title} data-editor-test-double />;
}
