type UuidRandomSource = {
  getRandomValues: (bytes: Uint8Array) => Uint8Array;
  randomUUID?: () => string;
};

const UUID_BYTE_LENGTH = 16;
const UUID_VERSION_BYTE_INDEX = 6;
const UUID_VARIANT_BYTE_INDEX = 8;

/** Browser環境に依存せず、暗号学的乱数からUUID v4を生成する。 */
export function createClientUuid(
  randomSource: UuidRandomSource = crypto,
): string {
  const nativeRandomUuid = randomSource.randomUUID;
  if (typeof nativeRandomUuid === "function") {
    return nativeRandomUuid.call(randomSource);
  }

  // LAN上のHTTPではrandomUUIDが公開されないため、利用可能な同じ乱数源から組み立てる。
  const bytes = randomSource.getRandomValues(
    new Uint8Array(UUID_BYTE_LENGTH),
  );
  bytes[UUID_VERSION_BYTE_INDEX] =
    (bytes[UUID_VERSION_BYTE_INDEX]! & 0x0f) | 0x40;
  bytes[UUID_VARIANT_BYTE_INDEX] =
    (bytes[UUID_VARIANT_BYTE_INDEX]! & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}
