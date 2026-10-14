//
// Contract for non-volatile storage whose contents a test supplies and reads
// back as a raw image file (board.yaml storage.<name>.peripheral).
//
namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    /// <summary>Non-volatile storage backed by a raw image file: byte n of the
    /// file is the byte at device address n.</summary>
    public interface IStorage : IPeripheral
    {
        /// <summary>Capacity in bytes. board.yaml storage.&lt;name&gt;.size must
        /// equal it.</summary>
        long Size { get; }

        /// <summary>Makes the file at <paramref name="path"/> the device's
        /// backing store. A file that exists and is not empty becomes the
        /// device's contents; it may not be longer than <see cref="Size"/>
        /// (RecoverableException), and addresses past its end read as erased
        /// (0xFF for NOR flash). A missing or empty file takes the device's
        /// current contents. Afterwards the file is <see cref="Size"/> bytes
        /// long, and every change the device makes (a program, an erase) is
        /// written to it as it completes, so the file holds the contents when the
        /// run ends. Contents survive Reset and PowerCycle. A second call moves
        /// the device to the new file; the first keeps what was written to
        /// it.</summary>
        void AttachImage(string path);
    }
}
