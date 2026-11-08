import XCTest
@testable import TimeCapsuleSMBApp

final class LinkedTextTests: XCTestCase {
    private func links(in text: String) -> [(String, URL)] {
        let attributed = AttributedString.linkingWebAddresses(in: text)
        return attributed.runs.compactMap { run in
            guard let url = run.link else { return nil }
            return (String(attributed[run.range].characters), url)
        }
    }

    func testAWebAddressBecomesALinkWithoutTheSentencesPeriod() {
        let guide = "https://github.com/jamesyc/TimeCapsuleSMB/blob/main/FAQ.md#a-seagate-disk-reports-386-gb"
        let found = links(in: "Replace the disk. For a replacement guide, see \(guide).")
        XCTAssertEqual(found.map(\.0), [guide])
        XCTAssertEqual(found.map(\.1), [URL(string: guide)!])
    }

    func testEveryAddressInALineIsLinkedAndTheTextIsUnchanged() {
        let thread = "https://forums.tomshardware.com/threads/seagate-1tb-st1000vm002-unallocated-3-86-gb.3657800/"
        let text = "Other owners describe the same failure at \(thread). Report it at https://github.com/jamesyc/TimeCapsuleSMB/issues."
        XCTAssertEqual(links(in: text).map(\.0), [thread, "https://github.com/jamesyc/TimeCapsuleSMB/issues"])
        XCTAssertEqual(String(AttributedString.linkingWebAddresses(in: text).characters), text)
    }

    func testFullWidthPunctuationEndsTheAddress() {
        let guide = "https://github.com/jamesyc/TimeCapsuleSMB/blob/main/FAQ.md#a-seagate-disk-reports-386-gb"
        XCTAssertEqual(links(in: "请更换磁盘。更换指南见 \(guide)。").map(\.0), [guide])
        XCTAssertEqual(links(in: "请更换磁盘（更换指南：\(guide)）。").map(\.0), [guide])
    }

    func testParenthesesAroundAnAddressStayOutOfIt() {
        // The catalogs put each address in parentheses, so neither the
        // closing one nor the period after it reaches the link.
        let thread = "https://forums.tomshardware.com/threads/seagate-1tb-st1000vm002-unallocated-3-86-gb.3657800/"
        let guide = "https://github.com/jamesyc/TimeCapsuleSMB/blob/main/FAQ.md#a-seagate-disk-reports-386-gb"
        XCTAssertEqual(links(in: "Other owners describe the same failure on Tom's Hardware (\(thread)).").map(\.0), [thread])
        XCTAssertEqual(links(in: "Remplacez le disque (guide de remplacement\u{00A0}: \(guide)).").map(\.0), [guide])
    }

    func testEveryLineOfTheSeagateCardIsLinkedInEveryLanguage() {
        // The URLs reach each catalog by hand: each must come out whole, as
        // the one address on its line.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let thread = "https://forums.tomshardware.com/threads/seagate-1tb-st1000vm002-unallocated-3-86-gb.3657800/"
        let guide = "https://github.com/jamesyc/TimeCapsuleSMB/blob/main/FAQ.md#a-seagate-disk-reports-386-gb"
        for language in AppLanguage.allCases where language != .system {
            L10n.apply(language: language)
            for (key, url) in [("doctor.check.disk_seagate_failed", guide),
                               ("backend.recovery.disk_seagate_failed.action.1", thread),
                               ("backend.recovery.disk_seagate_failed.action.2", guide)] {
                XCTAssertEqual(links(in: L10n.string(key)).map(\.1), [URL(string: url)!], "\(language) \(key)")
            }
        }
    }

    func testHostNamesFileNamesAndOtherSchemesStayText() {
        let text = "SMB mount failed for admin@127.0.0.1/Data on airport.local; check rc.local, "
            + "mail admin@example.com or open example.com and ftp://example.com/file."
        XCTAssertTrue(links(in: text).isEmpty)
    }

    func testTextWithoutAddressesHasNoLinks() {
        XCTAssertTrue(links(in: "Replace the disk.").isEmpty)
        XCTAssertTrue(links(in: "").isEmpty)
    }
}
