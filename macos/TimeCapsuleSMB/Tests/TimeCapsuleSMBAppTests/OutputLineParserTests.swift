import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class OutputLineParserTests: XCTestCase {
    func testParserHandlesSplitMultipleAndUnterminatedLines() {
        var events: [BackendEvent] = []
        let parser = OutputLineParser { events.append($0) }

        parser.append(Data(#"{"type":"stage","operation":"paths","stage":"resolve"#.utf8))
        parser.append(Data(#"_paths"}"#.utf8))
        parser.append(Data("\nnot-json\n".utf8))
        parser.append(Data(#"{"type":"result","operation":"paths","ok":true,"payload":{}}"#.utf8))
        parser.finish()

        XCTAssertEqual(events.map(\.type), ["stage", "result"])
        XCTAssertEqual(events.first?.stage, "resolve_paths")
        XCTAssertEqual(events.last?.ok, true)
    }
}
