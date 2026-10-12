import Foundation

/// Apple's mDNSCore renames a Bonjour service name that another device on the
/// link already uses: "Name" becomes "Name (2)" (DNSCommon.c
/// IncrementLabelSuffix, unchanged since mDNSResponder-108). The helper's
/// probe.apple_conflict_name is the same rule; both are checked against
/// Apple's own code from the firmware in Fixtures/apple_mdns_names.json.
///
/// An existing " (N)" is taken off and its number raised (from 10 at random).
/// Trailing spaces go before the suffix, and the name is cut on a UTF-8
/// boundary so it and the suffix fit 63 bytes. Apple never saves a rename, and
/// whoever registers first after a restart keeps the plain name, so two
/// devices with one name can swap names. Host labels ("name-2") are left to
/// the helper: "Office 2" also becomes Office-2, so a -N proves nothing here.
enum AppleBonjourName {
    private static let maxLabelBytes = 63

    /// A name as mDNSCore compares it: SameDomainLabel (ACPd 0x2ba0b8 4 LE,
    /// 0x2e9a44 NetBSD 6) folds ASCII A-Z and compares every other byte as is,
    /// as RFC 6762 section 16 says. "Été" and "été", or an "é" written as "e"
    /// plus an accent, are other names to Bonjour, so no Unicode case folding
    /// or normalization here (Swift's == would treat the last two as equal).
    static func key(_ name: String) -> [UInt8] {
        name.utf8.map { (0x41...0x5A).contains($0) ? $0 + 0x20 : $0 }
    }

    /// Whether the name ends in the " (N)" Apple adds after a conflict. Its
    /// numbers start at 2, so "Office (1)" is a name of its own.
    static func hasConflictSuffix(_ name: String) -> Bool {
        (suffix(of: Array(name.utf8))?.number ?? 0) >= 2
    }

    /// What two names collide on: the name without Apple's suffix and the
    /// spaces it drops before one.
    static func conflictBase(_ name: String) -> String {
        let bytes = Array(name.utf8)
        let found = suffix(of: bytes).flatMap { $0.number >= 2 ? $0.base : nil }
        return String(decoding: trimmingTrailingSpaces(found ?? bytes[...]), as: UTF8.self)
    }

    /// The name Apple gives `name` once its conflict number reaches `number` (AppendLabelSuffix).
    static func conflictName(_ name: String, number: Int) -> String {
        let bytes = Array(name.utf8)
        let base = trimmingTrailingSpaces(suffix(of: bytes)?.base ?? bytes[...])
        let text = Array(" (\(number))".utf8)
        return String(decoding: truncated(base, to: maxLabelBytes - text.count) + text, as: UTF8.self)
    }

    /// Whether Apple renames `original` to `advertised` after name conflicts.
    /// The number only goes up, so a name already ending in one is never
    /// renamed to a lower one.
    static func isConflictRename(_ advertised: String, of original: String) -> Bool {
        guard let renamed = suffix(of: Array(advertised.utf8)) else { return false }
        let own = suffix(of: Array(original.utf8))?.number ?? 0
        guard renamed.number >= max(2, own + 1) else { return false }
        return conflictName(original, number: renamed.number) == advertised
    }

    private static func isDigit(_ byte: UInt8) -> Bool { (0x30...0x39).contains(byte) }

    /// The label before " (N)" and N (LabelContainsSuffix, RemoveLabelSuffix).
    private static func suffix(of label: [UInt8]) -> (base: ArraySlice<UInt8>, number: Int)? {
        let end = label.count
        guard end >= 4, label[end - 1] == 0x29, isDigit(label[end - 2]) else { return nil }
        var start = end - 2
        while start > 2 && isDigit(label[start - 1]) { start -= 1 }
        guard label[start - 1] == 0x28, label[start - 2] == 0x20,
              let number = Int(String(decoding: label[start..<(end - 1)], as: UTF8.self)) else { return nil }
        return (label[..<(start - 2)], number)
    }

    private static func trimmingTrailingSpaces(_ label: ArraySlice<UInt8>) -> ArraySlice<UInt8> {
        var end = label.endIndex
        while end > label.startIndex && label[end - 1] == 0x20 { end -= 1 }
        return label[label.startIndex..<end]
    }

    /// TruncateUTF8ToLength: cut on a character boundary, then drop trailing whitespace.
    private static func truncated(_ label: ArraySlice<UInt8>, to maxBytes: Int) -> [UInt8] {
        let bytes = Array(label)
        guard bytes.count > maxBytes else { return bytes }
        var length = maxBytes
        var after = bytes[length]
        var following: UInt8 = length + 1 < bytes.count ? bytes[length + 1] : 0xB0
        while length > 0 && ((after & 0xC0) == 0x80 || (after == 0xED && (following & 0xF0) == 0xB0)) {
            following = after
            length -= 1
            after = bytes[length]
        }
        while length > 0 && bytes[length - 1] <= 0x20 { length -= 1 }
        return Array(bytes[..<length])
    }
}
