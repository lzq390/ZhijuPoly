import assert from "node:assert/strict";

// Check the actual parent sidebar and the embedded November UI via user actions.
// The two M1 projects and attachment names intentionally match.
export async function checkVisibleWorkspace(page, frame, user, other) {
  const owner = "M1 synthetic " + user;
  const foreign = "M1 synthetic " + other;
  const parentSession = page.getByRole("button", { name: owner + " general", exact: true });
  const sameProject = page.locator('button[data-project-directory="/openscience/files/projects/same-project"]');

  const checkPrivate = async () => {
    assert.ok(!(await page.locator("body").innerText()).includes(foreign), "foreign content visible in the parent");
    assert.ok(!(await frame.locator("body").innerText()).includes(foreign), "foreign content visible in OpenScience");
  };
  await parentSession.waitFor({ state: "visible", timeout: 30000 });
  await parentSession.click();
  await frame.getByText(owner + " general prompt", {exact: true}).first().waitFor({timeout: 15000});
  await checkPrivate();

  const expand = page.getByRole("button", {name: "展开项目", exact: true});
  if (await expand.isVisible()) await expand.click();
  await sameProject.click({timeout: 20000});
  const projectSession = frame.getByText(owner + " project", {exact: true}).first();
  await projectSession.click({timeout: 15000});
  await frame.getByText(owner + " project prompt", {exact: true}).first().waitFor({timeout:15000});
  const attachment = frame.getByText("notebook.txt", {exact: true}).first();
  await frame.getByText(/^(Files|文件)$/).first().click({timeout:15000});
  await attachment.click({timeout:15000});
  await frame.getByText(owner + " project file", {exact: true}).first().waitFor({timeout:10000});
  await checkPrivate();
  return {parent_general_session:true, parent_project:true, general_prompt:true, project_prompt:true,
    project_attachment:"notebook.txt", attachment_file_contents:true, foreign_visible:false};
}
