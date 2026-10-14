//
// Contract for a device that loses power with the board (board.yaml
// power.restart). jig.contracts.verify checks that every peripheral listed
// there implements it.
//
namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    /// <summary>A device on one of the board's supply rails. The harness calls
    /// <see cref="PowerCycle"/> when it powers the board off and again when it
    /// powers the board on, so the device starts every powered period in its
    /// power-up state.</summary>
    public interface IPowerCycled : IPeripheral
    {
        /// <summary>Supply loss and return, at once and without simulated time.
        /// Every volatile register, latch, counter and buffer takes its power-up
        /// value, and an operation in progress (a program, an erase, a
        /// conversion) ends without its effect. Non-volatile contents (a flash
        /// array, the image file behind it) stay, and so do inputs that come from
        /// outside the device (an analog input voltage). A second call in a row
        /// has the effect of the first. Not to be confused with Reset, the
        /// board's reset line, which a device without a reset input ignores.</summary>
        void PowerCycle();
    }
}
