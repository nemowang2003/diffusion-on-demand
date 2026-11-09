"use strict";

// PNG is already compressed. ZIP's STORE method preserves the original bytes
// and avoids a dependency or a second compression pass.
async function createImageZip(files) {
  if (files.length > 65535)
    throw new Error("单次最多下载 65535 张图片，请分批选择。");
  const table = Uint32Array.from({ length: 256 }, (_, n) => {
    for (let i = 0; i < 8; i++) n = (n >>> 1) ^ (n & 1 ? 0xedb88320 : 0);
    return n >>> 0;
  });
  const parts = [];
  const directory = [];
  let offset = 0;
  let directorySize = 0;
  for (const file of files) {
    const bytes = new Uint8Array(await file.blob.arrayBuffer());
    let crc = 0xffffffff;
    for (const byte of bytes) crc = (crc >>> 8) ^ table[(crc ^ byte) & 255];
    crc = (crc ^ 0xffffffff) >>> 0;
    const name = new TextEncoder().encode(file.name);
    if (
      name.length > 65535 ||
      offset + bytes.length + name.length + 30 > 0xffffffff
    )
      throw new Error("所选图片超过 ZIP 的 4 GiB 上限，请分批下载。");
    const date = new Date(file.created);
    const year = Math.min(2107, Math.max(1980, date.getFullYear()));
    const time =
      (date.getHours() << 11) |
      (date.getMinutes() << 5) |
      (date.getSeconds() >> 1);
    const day =
      ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
    const local = new Uint8Array(30 + name.length);
    const view = new DataView(local.buffer);
    view.setUint32(0, 0x04034b50, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, 0x0800, true);
    view.setUint16(10, time, true);
    view.setUint16(12, day, true);
    view.setUint32(14, crc, true);
    view.setUint32(18, bytes.length, true);
    view.setUint32(22, bytes.length, true);
    view.setUint16(26, name.length, true);
    local.set(name, 30);
    const central = new Uint8Array(46 + name.length);
    const entry = new DataView(central.buffer);
    entry.setUint32(0, 0x02014b50, true);
    entry.setUint16(4, 20, true);
    entry.setUint16(6, 20, true);
    entry.setUint16(8, 0x0800, true);
    entry.setUint16(12, time, true);
    entry.setUint16(14, day, true);
    entry.setUint32(16, crc, true);
    entry.setUint32(20, bytes.length, true);
    entry.setUint32(24, bytes.length, true);
    entry.setUint16(28, name.length, true);
    entry.setUint32(42, offset, true);
    central.set(name, 46);
    parts.push(local, file.blob);
    directory.push(central);
    offset += local.length + bytes.length;
    directorySize += central.length;
  }
  if (offset + directorySize + 22 > 0xffffffff)
    throw new Error("所选图片超过 ZIP 的 4 GiB 上限，请分批下载。");
  const end = new Uint8Array(22);
  const view = new DataView(end.buffer);
  view.setUint32(0, 0x06054b50, true);
  view.setUint16(8, files.length, true);
  view.setUint16(10, files.length, true);
  view.setUint32(12, directorySize, true);
  view.setUint32(16, offset, true);
  return new Blob([...parts, ...directory, end], { type: "application/zip" });
}
