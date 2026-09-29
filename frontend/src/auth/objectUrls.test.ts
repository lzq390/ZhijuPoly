import { afterEach, expect, it, vi } from "vitest";
import { createPrivateObjectURL, revokePrivateObjectURL } from "./objectUrls";
import { getSessionEpoch, retireSession } from "./session";

afterEach(() => vi.restoreAllMocks());

it("revokes every outstanding private preview and download when the session retires", () => {
  vi.spyOn(URL, "createObjectURL").mockReturnValueOnce("blob:preview-a").mockReturnValueOnce("blob:download-a");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  createPrivateObjectURL(new Blob(["private-preview"]));
  createPrivateObjectURL(new Blob(["private-download"]));
  retireSession();
  expect(revoke.mock.calls).toEqual([["blob:preview-a"], ["blob:download-a"]]);
});

it("does not recreate an old image URL when local decoding finishes after a switch", () => {
  const epoch = getSessionEpoch();
  const create = vi.spyOn(URL, "createObjectURL");
  retireSession();
  expect(() => createPrivateObjectURL(new Blob(["private-a"]), epoch)).toThrow(/登录状态已改变/);
  expect(create).not.toHaveBeenCalled();
});

it("removes normally disposed URLs from session cleanup", () => {
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:disposed");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  revokePrivateObjectURL(createPrivateObjectURL(new Blob()));
  retireSession();
  expect(revoke).toHaveBeenCalledOnce();
});
