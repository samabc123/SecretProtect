package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/labadmission"
)

func main() {
	if !labadmission.Enabled() {
		log.Fatal("LAB_ADMISSION_ENABLED must be explicitly enabled")
	}
	engine, err := labadmission.Open(os.Getenv("LAB_ADMISSION_DATABASE_URL"))
	if err != nil {
		log.Fatal("admission worker configuration or database unavailable")
	}
	defer engine.DB.Close()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		count, err := engine.SyncOnce(ctx)
		if err != nil && ctx.Err() == nil {
			log.Print("admission queue unavailable; access leases will expire closed")
		} else if count > 0 {
			log.Printf("admission queue checked: %d", count)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
