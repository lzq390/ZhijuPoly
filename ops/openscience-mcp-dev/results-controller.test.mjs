import test from "node:test"
import assert from "node:assert/strict"
import { managedDirectory, sessionFromPath, sessionRequest } from "./results-controller.mjs"

test("route selection binds only the actual chat session", () => {
  assert.equal(sessionFromPath("/chat/ses_e60eb9693ffei3Y8AvJIX0I20T"), "ses_e60eb9693ffei3Y8AvJIX0I20T")
  for (const route of ["/", "/chat/new", "/other/chat/ses_valid", "/chat/ses_valid/message", "/chat/ses_%2Fprivate"]) {
    assert.equal(sessionFromPath(route), null)
  }
})

test("directory context stays inside managed workspaces", () => {
  for (const directory of ["/openscience/files/general", "/openscience/files/projects/demo"]) {
    assert.equal(managedDirectory(directory), directory)
  }
  for (const directory of [null, "/run/openscience", "/openscience/files/general-other", "/openscience/files/projects/../private", "/openscience/files/projects/x\n"]) {
    assert.equal(managedDirectory(directory), null)
  }
})

test("fetch observation accepts only same-origin session responses and exact directory headers", () => {
  const origin = "http://127.0.0.1:19111"
  const path = "/openscience-api/session/ses_valid/message?limit=400"
  const options = { headers: { "x-openscience-directory": "/openscience/files/projects/demo" } }
  assert.deepEqual(sessionRequest(path, options, origin), {
    sessionId: "ses_valid", directory: "/openscience/files/projects/demo", messages: true,
  })
  assert.equal(sessionRequest("http://outside.example" + path, options, origin), null)
  assert.equal(sessionRequest("/session/ses_valid/message", options, origin), null)
  assert.equal(sessionRequest(path, { method: "POST" }, origin).messages, false)
  const request = new Request(origin + path, options)
  assert.equal(sessionRequest(request, undefined, origin).directory, "/openscience/files/projects/demo")
  assert.equal(sessionRequest(request, { headers: {} }, origin).directory, null)
  assert.equal(sessionRequest(path + "&directory=%2Fopenscience%2Ffiles%2Fgeneral", options, origin).directory,
    "/openscience/files/general")
  assert.equal(sessionRequest(path + "&directory=%2Frun%2Fopenscience", options, origin).directory, null)
})
