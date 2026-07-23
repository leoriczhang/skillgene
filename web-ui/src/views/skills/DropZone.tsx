import { useRef, useState } from "react";
import { cn } from "@/lib/utils";

/**
 * Click-or-drag file drop zone. Mirrors console.html wireDrop():
 * - multiple=true  → passes the FileList to onFiles
 * - multiple=false → passes a single File to onFiles
 */
export default function DropZone({
  onFiles,
  label,
  multiple = false,
  accept,
}: {
  onFiles: (files: FileList) => void;
  label: string;
  multiple?: boolean;
  accept?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  return (
    <>
      <div
        className={cn("drop", over && "over")}
        onClick={() => inputRef.current?.click()}
        onDragEnter={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          setOver(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (e.dataTransfer.files.length) onFiles(e.dataTransfer.files);
        }}
      >
        {label}
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple={multiple}
        accept={accept}
        className="hidden"
        onChange={(e) => {
          if (e.target.files?.length) onFiles(e.target.files);
          e.target.value = "";
        }}
      />
    </>
  );
}
