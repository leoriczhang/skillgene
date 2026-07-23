import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toastOk, toastErr } from "@/lib/toast";
import { api, cloudNote } from "@/api/client";
import DropZone from "./DropZone";
import { fileToB64 } from "@/lib/file";

export default function ImportZipModal({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: () => void;
}) {
  const [zipB64, setZipB64] = useState<string | null>(null);
  const [zipName, setZipName] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setZipB64(null);
      setZipName("");
      setInfo("");
      setBusy(false);
    }
  }, [open]);

  async function pickZip(files: FileList) {
    const file = files[0];
    if (!file) return;
    setZipB64(await fileToB64(file));
    setInfo(`已选择：${file.name} (${(file.size / 1024).toFixed(1)} KB)`);
  }

  async function importZip() {
    if (!zipB64) return;
    setBusy(true);
    try {
      const r = await api<{ created?: boolean; name: string; cloud?: any }>(
        "/api/skills/import-zip",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ zip_b64: zipB64, name: zipName.trim() }),
        }
      );
      toastOk(r.created ? "已导入新技能" : "已覆盖技能", `${r.name} · ${cloudNote(r.cloud)}`);
      onClose();
      onImported();
    } catch (e: any) {
      toastErr("导入失败", e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="w-full !max-w-[560px]">
        <DialogHeader>
          <DialogTitle>上传 .zip 技能包</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
              名称覆盖（可选，默认取包内 SKILL.md 的 name）
            </label>
            <Input
              value={zipName}
              placeholder="留空则自动识别"
              onChange={(e) => setZipName(e.target.value)}
            />
          </div>
          <DropZone accept=".zip" onFiles={pickZip} label="点击或拖拽 .zip 到此处" />
          {info && <div className="text-xs text-muted-foreground">{info}</div>}
        </div>

        <DialogFooter className="!bg-transparent !px-0 !py-0">
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button disabled={!zipB64 || busy} onClick={importZip}>
            导入
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
