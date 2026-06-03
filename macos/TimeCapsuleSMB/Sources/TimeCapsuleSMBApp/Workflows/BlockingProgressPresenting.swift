import Foundation

protocol BlockingProgressPresenting {
    var title: String { get }
    var message: String { get }
    var detail: String? { get }
}
