import XCTest
@testable import TimeCapsuleSMBApp

final class ReleaseNotesMarkdownTests: XCTestCase {
    func testParsesHeadingsBulletsAndParagraphs() {
        let markdown = "### Title\n\n- one\n  - nested\n* two\n\nPlain **bold** text\ncontinues\n"

        XCTAssertEqual(ReleaseNotesMarkdown.blocks(from: markdown), [
            .heading(level: 3, text: "Title"),
            .bullet(level: 0, text: "one"),
            .bullet(level: 1, text: "nested"),
            .bullet(level: 0, text: "two"),
            .paragraph("Plain **bold** text continues")
        ])
    }

    func testEmptyBodyProducesNoBlocks() {
        XCTAssertEqual(ReleaseNotesMarkdown.blocks(from: "  \n\n"), [])
        XCTAssertEqual(String(ReleaseNotesMarkdown.attributedString(from: "").characters), "")
    }

    func testHashWithoutTextAndDashWithoutSpaceAreParagraphs() {
        XCTAssertEqual(ReleaseNotesMarkdown.blocks(from: "###\n-not a bullet"), [
            .paragraph("### -not a bullet")
        ])
    }

    func testAttributedStringRendersInlineMarkdown() {
        let text = ReleaseNotesMarkdown.attributedString(from: "## Changes\n- **bold** item\n\n[link](https://example.invalid) here")
        let plain = String(text.characters)

        XCTAssertTrue(plain.hasPrefix("Changes\n•  bold item\nlink here"), plain)
        XCTAssertFalse(plain.contains("**"))
        XCTAssertFalse(plain.contains("]("))
    }
}
