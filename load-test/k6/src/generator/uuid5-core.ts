const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/;
const ASCII_NAME_PATTERN = /^[\x20-\x7e]{1,200}(?![\s\S])/;

export type Sha1Binary = (value: ArrayBuffer) => ArrayBuffer;

/** k6/cryptoを注入し、Node builtinなしでRFC 4122 UUIDv5を構築する。 */
export function uuid5Ascii(
  namespace: string,
  name: string,
  sha1: Sha1Binary,
): string {
  if (!UUID_PATTERN.test(namespace) || !ASCII_NAME_PATTERN.test(name)) {
    throw new Error("generator_uuid5_input_invalid");
  }
  const namespaceBytes = uuidBytes(namespace);
  const input = new Uint8Array(namespaceBytes.byteLength + name.length);
  input.set(namespaceBytes);
  for (let index = 0; index < name.length; index += 1) {
    input[namespaceBytes.byteLength + index] = name.charCodeAt(index);
  }

  const rawDigest = sha1(input.buffer);
  const digest = new Uint8Array(rawDigest);
  if (digest.byteLength !== 20) {
    digest.fill(0);
    input.fill(0);
    throw new Error("generator_uuid5_digest_invalid");
  }
  const uuid = digest.slice(0, 16);
  uuid[6] = ((uuid[6] ?? 0) & 0x0f) | 0x50;
  uuid[8] = ((uuid[8] ?? 0) & 0x3f) | 0x80;
  const result = formatUuid(uuid);
  uuid.fill(0);
  digest.fill(0);
  input.fill(0);
  namespaceBytes.fill(0);
  return result;
}

function uuidBytes(value: string): Uint8Array {
  const hex = value.replaceAll("-", "");
  const result = new Uint8Array(16);
  for (let index = 0; index < result.byteLength; index += 1) {
    const octet = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
    if (!Number.isInteger(octet)) {
      result.fill(0);
      throw new Error("generator_uuid5_input_invalid");
    }
    result[index] = octet;
  }
  return result;
}

function formatUuid(bytes: Uint8Array): string {
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
