import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class OutputLineParserTests: XCTestCase {
    func testParserHandlesSplitMultipleAndUnterminatedLines() {
        var parser = OutputLineParser()

        var events: [BackendEvent] = []
        events.append(contentsOf: parser.append(Data(#"{"type":"stage","operation":"paths","stage":"resolve"#.utf8)))
        events.append(contentsOf: parser.append(Data(#"_paths"}"#.utf8)))
        events.append(contentsOf: parser.append(Data("\nnot-json\n".utf8)))
        events.append(contentsOf: parser.append(Data(#"{"type":"result","operation":"paths","ok":true,"payload":{}}"#.utf8)))
        events.append(contentsOf: parser.finish())

        XCTAssertEqual(events.map(\.type), ["stage", "result"])
        XCTAssertEqual(events.first?.stage, "resolve_paths")
        XCTAssertEqual(events.last?.ok, true)
    }
}
