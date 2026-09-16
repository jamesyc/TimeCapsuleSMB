import Foundation
import SwiftUI

/// Minimal renderer for GitHub release-note markdown: headings, bullet lists and paragraphs,
/// with Foundation's inline markdown (bold, italic, code, links) applied per line.
/// Anything else is shown as plain text rather than dropped.
enum ReleaseNotesMarkdown {
    enum Block: Equatable {
        case heading(level: Int, text: String)
        case bullet(level: Int, text: String)
        case paragraph(String)
    }

    static func blocks(from markdown: String) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []

        func flushParagraph() {
            guard !paragraph.isEmpty else {
                return
            }
            blocks.append(.paragraph(paragraph.joined(separator: " ")))
            paragraph = []
        }

        for rawLine in markdown.components(separatedBy: .newlines) {
            let trimmed = rawLine.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty {
                flushParagraph()
                continue
            }
            if trimmed.hasPrefix("#") {
                let level = trimmed.prefix(while: { $0 == "#" }).count
                let text = trimmed.dropFirst(level).trimmingCharacters(in: .whitespaces)
                if level <= 6, !text.isEmpty {
                    flushParagraph()
                    blocks.append(.heading(level: level, text: text))
                    continue
                }
            }
            if let marker = trimmed.first,
               "-*+".contains(marker),
               trimmed.dropFirst().first == " " {
                let indent = rawLine.prefix(while: { $0 == " " || $0 == "\t" }).count
                let text = trimmed.dropFirst(2).trimmingCharacters(in: .whitespaces)
                flushParagraph()
                blocks.append(.bullet(level: min(indent / 2, 3), text: text))
                continue
            }
            paragraph.append(trimmed)
        }
        flushParagraph()
        return blocks
    }

    static func attributedString(from markdown: String) -> AttributedString {
        var result = AttributedString()
        for (index, block) in blocks(from: markdown).enumerated() {
            if index > 0 {
                result.append(AttributedString("\n"))
            }
            switch block {
            case .heading(let level, let text):
                var heading = inline(text)
                heading.font = level <= 2 ? Font.title3.bold() : Font.headline
                result.append(heading)
            case .bullet(let level, let text):
                let indent = String(repeating: "    ", count: level)
                result.append(AttributedString(indent + "•  "))
                result.append(inline(text))
            case .paragraph(let text):
                result.append(inline(text))
            }
        }
        return result
    }

    private static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}
