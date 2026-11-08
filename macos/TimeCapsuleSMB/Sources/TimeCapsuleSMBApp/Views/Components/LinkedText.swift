import Foundation

/// One detector for every line: Checkup lists dozens of rows, and a detector
/// is immutable, so sharing it across threads is safe.
private let webAddressDetector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)

extension AttributedString {
    /// The text with each web address in it a link. Recovery steps and Checkup
    /// lines name pages to open (a GitHub issue, a guide), and plain Text
    /// leaves them inert. Only addresses spelled with http:// or https://
    /// become links: the data detector also takes host names such as
    /// `airport.local` or a file name for addresses.
    static func linkingWebAddresses(in text: String) -> AttributedString {
        var result = AttributedString(text)
        guard let detector = webAddressDetector else {
            return result
        }
        let whole = NSRange(text.startIndex..<text.endIndex, in: text)
        for match in detector.matches(in: text, range: whole) {
            guard let url = match.url,
                  url.scheme == "http" || url.scheme == "https",
                  let range = Range(match.range, in: text),
                  text[range].lowercased().hasPrefix("http"),
                  let lower = AttributedString.Index(range.lowerBound, within: result),
                  let upper = AttributedString.Index(range.upperBound, within: result) else {
                continue
            }
            result[lower..<upper].link = url
        }
        return result
    }
}
